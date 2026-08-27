"""NATS JetStream consumer — thin adapter, mirrors the ``/apply/*`` HTTP routes.

Subscribes to the ``aiac-agent-consumer`` durable queue group and, on each
message, calls the same use-case handler + ``compute_and_apply`` sequence the
HTTP routes use, awaiting completion before acking. On handler failure, the
message is left unacked (NATS redelivers) until ``num_delivered`` reaches
``MAX_DELIVER``, at which point it is republished to the DLQ subject and
terminated (stops redelivery on this consumer).
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from urllib.parse import unquote

import nats
from fastapi import FastAPI
from nats.aio.msg import Msg
from nats.js.api import AckPolicy, ConsumerConfig

from aiac.agent.eventbus.stream import (
    ACK_WAIT_SECONDS,
    CONSUMER_FILTER_SUBJECTS,
    CONSUMER_NAME,
    DEFAULT_NATS_URL,
    DLQ_SUBJECT,
    MAX_DELIVER,
    STREAM_NAME,
    ensure_stream,
)
from aiac.agent.uc.onboarding.orchestrator import onboard_service
from aiac.agent.uc.policy_update.build import build_policy
from aiac.agent.uc.role_update.role import update_role
from aiac.policy.computation import compute_and_apply
from aiac.policy.model.models import PolicyRule, RuleEffect

logger = logging.getLogger(__name__)

NATS_URL = os.environ.get("NATS_URL", DEFAULT_NATS_URL)

_START_RETRY_INITIAL_BACKOFF = 1.0
_START_RETRY_MAX_BACKOFF = 30.0

_SERVICE_PREFIX = "aiac.apply.service."
_ROLE_PREFIX = "aiac.apply.role."
_POLICY_BUILD_SUBJECT = "aiac.apply.policy.build"


def _handle(subject: str) -> tuple[list[PolicyRule], bool, RuleEffect]:
    # Normalize every handler to ``(rules, override, default_effect)``. Only onboarding carries a
    # caller-requestable ``default_effect``; the others always emit least-privilege ``DENY``.
    if subject.startswith(_SERVICE_PREFIX):
        return onboard_service(subject[len(_SERVICE_PREFIX) :])
    if subject.startswith(_ROLE_PREFIX):
        # Mirror image of the Keycloak SPI's SubjectMapper.encodeSubjectToken: role names may
        # contain '.', which NATS treats as a token separator, so the SPI percent-encodes them
        # into a single token before publishing. unquote() is the general-purpose inverse; safe
        # here because every literal '%' in the original name was itself escaped to "%25".
        rules, override = update_role(unquote(subject[len(_ROLE_PREFIX) :]))
        return rules, override, RuleEffect.DENY
    if subject == _POLICY_BUILD_SUBJECT:
        rules, override = build_policy()
        return rules, override, RuleEffect.DENY
    raise ValueError(f"no handler for subject {subject!r}")


class AiacEventConsumer:
    def __init__(self, nats_url: str = NATS_URL) -> None:
        self._nats_url = nats_url
        self._nc: nats.aio.client.Client | None = None
        self._sub = None

    async def start(self) -> None:
        # max_reconnect_attempts=-1: once connected, never give up on a dropped connection
        # (nats-py's own default is a bounded 60 attempts at a fixed reconnect_time_wait, not
        # indefinite). This only covers post-connect drops — the exponential backoff is
        # start_with_retry's, for retrying this initial connect/subscribe sequence itself.
        self._nc = await nats.connect(self._nats_url, max_reconnect_attempts=-1)
        js = self._nc.jetstream()
        await ensure_stream(js)
        self._sub = await js.subscribe(
            subject="aiac.apply.>",
            queue=CONSUMER_NAME,
            durable=CONSUMER_NAME,
            stream=STREAM_NAME,
            manual_ack=True,
            cb=self._dispatch,
            config=ConsumerConfig(
                filter_subjects=CONSUMER_FILTER_SUBJECTS,
                ack_policy=AckPolicy.EXPLICIT,
                max_deliver=MAX_DELIVER,
                ack_wait=ACK_WAIT_SECONDS,
            ),
        )
        logger.info("aiac-agent-consumer subscribed to %s", CONSUMER_FILTER_SUBJECTS)

    async def start_with_retry(self) -> None:
        """Retry ``start()`` with exponential backoff until it succeeds.

        Without this, a failure anywhere in ``start()`` (connect, stream provisioning,
        subscribe) leaves the background task dead with nothing to restart it — the
        Controller keeps serving ``/health`` as if event consumption were live.
        """
        backoff = _START_RETRY_INITIAL_BACKOFF
        while True:
            try:
                await self.start()
                return
            except Exception:
                logger.exception("consumer failed to start; retrying in %.0fs", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _START_RETRY_MAX_BACKOFF)

    async def stop(self) -> None:
        if self._sub is not None:
            await self._sub.unsubscribe()
        if self._nc is not None:
            await self._nc.close()

    async def _dispatch(self, msg: Msg) -> None:
        try:
            rules, override, default_effect = _handle(msg.subject)
            compute_and_apply(rules, override, default_effect)
        except Exception:
            logger.exception("failed to process %s", msg.subject)
            if msg.metadata.num_delivered >= MAX_DELIVER:
                # jetstream().publish() waits for the broker's PubAck and raises on failure —
                # unlike the raw client's fire-and-forget publish() — so the message is only
                # terminated once the DLQ write is confirmed persisted.
                await self._nc.jetstream().publish(DLQ_SUBJECT, msg.data)
                await msg.term()
                logger.error(
                    "moved %s to %s after %d deliveries",
                    msg.subject,
                    DLQ_SUBJECT,
                    msg.metadata.num_delivered,
                )
            return
        await msg.ack()


@asynccontextmanager
async def lifespan(app: FastAPI):
    consumer = AiacEventConsumer()
    # Backgrounded so a slow NATS handshake never blocks /apply/* from becoming
    # available. Each individual message is still awaited to completion by
    # nats-py before this consumer's own ack/term call — see _dispatch.
    task = asyncio.create_task(consumer.start_with_retry())
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await consumer.stop()
