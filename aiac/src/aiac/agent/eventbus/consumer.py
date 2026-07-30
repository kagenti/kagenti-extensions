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

import nats
from fastapi import FastAPI
from nats.aio.msg import Msg
from nats.js.api import AckPolicy, ConsumerConfig

from aiac.agent.eventbus.stream import (
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
from aiac.policy.model.models import PolicyRule

logger = logging.getLogger(__name__)

NATS_URL = os.environ.get("NATS_URL", DEFAULT_NATS_URL)

_SERVICE_PREFIX = "aiac.apply.service."
_ROLE_PREFIX = "aiac.apply.role."
_POLICY_BUILD_SUBJECT = "aiac.apply.policy.build"


def _handle(subject: str) -> tuple[list[PolicyRule], bool]:
    if subject.startswith(_SERVICE_PREFIX):
        return onboard_service(subject[len(_SERVICE_PREFIX) :])
    if subject.startswith(_ROLE_PREFIX):
        return update_role(subject[len(_ROLE_PREFIX) :])
    if subject == _POLICY_BUILD_SUBJECT:
        return build_policy()
    raise ValueError(f"no handler for subject {subject!r}")


class AiacEventConsumer:
    def __init__(self, nats_url: str = NATS_URL) -> None:
        self._nats_url = nats_url
        self._nc: nats.aio.client.Client | None = None
        self._sub = None

    async def start(self) -> None:
        self._nc = await nats.connect(self._nats_url)
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
            ),
        )
        logger.info("aiac-agent-consumer subscribed to %s", CONSUMER_FILTER_SUBJECTS)

    async def stop(self) -> None:
        if self._sub is not None:
            await self._sub.unsubscribe()
        if self._nc is not None:
            await self._nc.close()

    async def _dispatch(self, msg: Msg) -> None:
        try:
            rules, override = _handle(msg.subject)
            compute_and_apply(rules, override)
        except Exception:
            logger.exception("failed to process %s", msg.subject)
            if msg.metadata.num_delivered >= MAX_DELIVER:
                await self._nc.publish(DLQ_SUBJECT, msg.data)
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
    task = asyncio.create_task(consumer.start())
    try:
        yield
    finally:
        task.cancel()
        await consumer.stop()
