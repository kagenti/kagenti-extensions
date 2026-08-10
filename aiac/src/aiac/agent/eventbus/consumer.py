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
from tenacity import AsyncRetrying, retry_if_exception, wait_exponential

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
from aiac.shared.upstream import is_transient

logger = logging.getLogger(__name__)

NATS_URL = os.environ.get("NATS_URL", DEFAULT_NATS_URL)

_SERVICE_PREFIX = "aiac.apply.service."
_ROLE_PREFIX = "aiac.apply.role."
_POLICY_BUILD_SUBJECT = "aiac.apply.policy.build"

# Initial-connect backoff (module-level so tests can zero them for fast retries).
# The connect loop retries forever on transient failures — there is no stop, so a
# broker that is down at boot is waited out rather than crashing the consumer.
CONNECT_BACKOFF_MULTIPLIER = 1
CONNECT_BACKOFF_MIN = 1
CONNECT_BACKOFF_MAX = 30

# Post-connect reconnect options handed to nats.connect: reconnect indefinitely,
# 2s between attempts. Push subscriptions auto-resume on reconnect.
_RECONNECT_TIME_WAIT = 2
_MAX_RECONNECT_ATTEMPTS = -1


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
        # Serializes _dispatch so at most one message is processed at a time,
        # preserving the pre-offload serial semantics (see _dispatch).
        self._lock = asyncio.Lock()

    async def _on_disconnected(self) -> None:
        logger.warning("nats disconnected; awaiting reconnect")

    async def _on_reconnected(self) -> None:
        logger.info("nats reconnected")

    async def _on_error(self, err: Exception) -> None:
        logger.error("nats client error: %s", err)

    async def _connect(self) -> "nats.aio.client.Client":
        """Connect to NATS, retrying transient failures with exponential backoff.

        Loops forever on transient errors (``is_transient``) so a broker that is
        down at boot is waited out rather than crashing the background task. Any
        non-transient error — including ``asyncio.CancelledError`` on shutdown —
        is not retried and propagates immediately, so cancellation exits cleanly.
        """
        async for attempt in AsyncRetrying(
            retry=retry_if_exception(is_transient),
            wait=wait_exponential(
                multiplier=CONNECT_BACKOFF_MULTIPLIER,
                min=CONNECT_BACKOFF_MIN,
                max=CONNECT_BACKOFF_MAX,
            ),
            reraise=True,
        ):
            with attempt:
                return await nats.connect(
                    self._nats_url,
                    max_reconnect_attempts=_MAX_RECONNECT_ATTEMPTS,
                    reconnect_time_wait=_RECONNECT_TIME_WAIT,
                    disconnected_cb=self._on_disconnected,
                    reconnected_cb=self._on_reconnected,
                    error_cb=self._on_error,
                )
        raise AssertionError("unreachable: AsyncRetrying exits via return or raise")

    async def start(self) -> None:
        self._nc = await self._connect()
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
            # The use-case handlers and the PCE are synchronous and can run for
            # the full LLM/OPA/HTTP duration. Offload them to a worker thread so
            # they never block the event loop (which would starve NATS
            # heartbeats/acks and /health). The lock keeps at most one message in
            # flight, preserving today's serial processing — no new concurrency.
            async with self._lock:
                rules, override = await asyncio.to_thread(_handle, msg.subject)
                await asyncio.to_thread(compute_and_apply, rules, override)
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
        # Await the cancelled task before stopping so a startup exception is
        # retrieved (no "Task exception was never retrieved" warning) and the
        # cancellation is observed to completion.
        await asyncio.gather(task, return_exceptions=True)
        await consumer.stop()
