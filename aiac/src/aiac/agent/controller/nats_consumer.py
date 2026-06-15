"""NATS JetStream consumer — thin adapter between the Event Broker and internal handlers."""

import asyncio
import logging
import os

try:
    import nats
except ImportError:  # nats-py not installed in test environment
    nats = None  # type: ignore

logger = logging.getLogger(__name__)

_STREAM = "aiac-events"
_CONSUMER_NAME_DEFAULT = "aiac-agent-consumer"


async def _dispatch(msg, *, policy_handler, role_handler, service_handler) -> None:
    """Dispatch a single NATS message to the appropriate handler, then ack on success."""
    subject: str = msg.subject
    handler = None
    kwargs: dict = {}

    if subject == "aiac.apply.policy.build":
        handler = policy_handler
        kwargs = {}
    elif subject.startswith("aiac.apply.role."):
        role_id = subject[len("aiac.apply.role."):]
        handler = role_handler
        kwargs = {"role_id": role_id}
    elif subject.startswith("aiac.apply.service."):
        service_id = subject[len("aiac.apply.service."):]
        handler = service_handler
        kwargs = {"service_id": service_id}
    # aiac.apply.policy.rebuild and unknown subjects: no handler — do not ack

    if handler is None:
        if subject not in ("aiac.apply.policy.rebuild",):
            logger.warning("Unrecognised NATS subject: %s — message not ack'd", subject)
        return

    try:
        await handler(**kwargs)
    except Exception:
        logger.exception("Handler for subject %s raised — message not ack'd for redelivery", subject)
        return

    await msg.ack()


async def _nats_consumer_loop(
    *,
    policy_handler,
    role_handler,
    service_handler,
    retry_delay: float = 5.0,
) -> None:
    """Connect to NATS, subscribe, and dispatch messages. Retries on connection loss."""
    nats_url = os.getenv("NATS_URL", "nats://localhost:4222")
    consumer_name = os.getenv("NATS_CONSUMER_NAME", _CONSUMER_NAME_DEFAULT)

    while True:
        nc = None
        try:
            nc = await nats.connect(nats_url)
            js = nc.jetstream()
            sub = await js.subscribe(
                "aiac.apply.>",
                stream=_STREAM,
                durable=consumer_name,
                queue=consumer_name,
            )
            logger.info("NATS consumer connected and subscribed")
            while True:
                msg = await sub.next_msg()
                await _dispatch(
                    msg,
                    policy_handler=policy_handler,
                    role_handler=role_handler,
                    service_handler=service_handler,
                )
        except asyncio.CancelledError:
            logger.info("NATS consumer loop cancelled — shutting down")
            if nc is not None:
                await nc.drain()
            raise
        except Exception:
            logger.exception("NATS consumer error — retrying in %.1fs", retry_delay)
            if retry_delay > 0:
                await asyncio.sleep(retry_delay)
