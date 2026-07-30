"""Shared NATS JetStream stream/consumer configuration.

Used both by the ``aiac-init`` init container (creates the stream) and the
Agent's own consumer startup (binds to it, and defensively re-runs the
idempotent ``add_stream`` call in case the init container hasn't run yet).
"""

import logging

from nats.js import JetStreamContext
from nats.js.api import RetentionPolicy, StreamConfig
from nats.js.errors import BadRequestError

logger = logging.getLogger(__name__)

DEFAULT_NATS_URL = "nats://aiac-event-broker-service:4222"

STREAM_NAME = "aiac-events"
STREAM_SUBJECTS = ["aiac.apply.>"]

CONSUMER_NAME = "aiac-agent-consumer"
# Deliberately narrower than the stream's own subjects: the stream captures
# every aiac.apply.> publish (including aiac.apply.dlq, so dead-lettered
# messages stay inspectable), but the consumer must not resubscribe to its
# own DLQ subject or it would reprocess dead-lettered messages forever.
CONSUMER_FILTER_SUBJECTS = [
    "aiac.apply.service.*",
    "aiac.apply.role.*",
    "aiac.apply.policy.build",
]

DLQ_SUBJECT = "aiac.apply.dlq"
MAX_DELIVER = 5

# JetStream's "stream name already in use with a different configuration" error code.
_STREAM_CONFIG_MISMATCH_ERR_CODE = 10058


async def ensure_stream(js: JetStreamContext) -> None:
    """Idempotently create the ``aiac-events`` stream.

    ``add_stream`` is itself idempotent (no-op success when the stream already
    exists with an identical config); this only needs to swallow the specific
    "already exists with a different config" error, since that's not our
    scenario here — the config below is the single source of truth for both
    callers.
    """
    try:
        await js.add_stream(
            config=StreamConfig(
                name=STREAM_NAME,
                subjects=STREAM_SUBJECTS,
                retention=RetentionPolicy.WORK_QUEUE,
            )
        )
        logger.info("aiac-events stream ready (name=%s, subjects=%s)", STREAM_NAME, STREAM_SUBJECTS)
    except BadRequestError as e:
        if e.err_code == _STREAM_CONFIG_MISMATCH_ERR_CODE:
            raise
        logger.info("aiac-events stream already exists: %s", e)
