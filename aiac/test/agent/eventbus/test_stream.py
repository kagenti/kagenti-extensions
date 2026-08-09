"""Unit tests for aiac.agent.eventbus.stream.ensure_stream.

``add_stream`` (mocked here as an AsyncMock JetStreamContext) is itself
idempotent — it only raises on a genuine "already exists with a different
configuration" error. These tests assert we re-raise that specific case and
swallow anything else.
"""

import asyncio
from unittest.mock import AsyncMock

from nats.js.api import RetentionPolicy
from nats.js.errors import BadRequestError

from aiac.agent.eventbus.stream import STREAM_NAME, STREAM_SUBJECTS, ensure_stream


def test_ensure_stream_creates_stream_with_expected_config():
    js = AsyncMock()

    asyncio.run(ensure_stream(js))

    _, kwargs = js.add_stream.call_args
    config = kwargs["config"]
    assert config.name == STREAM_NAME
    assert config.subjects == STREAM_SUBJECTS
    assert config.retention == RetentionPolicy.WORK_QUEUE


def test_ensure_stream_reraises_on_config_mismatch():
    js = AsyncMock()
    js.add_stream.side_effect = BadRequestError(err_code=10058)

    try:
        asyncio.run(ensure_stream(js))
    except BadRequestError:
        pass
    else:
        raise AssertionError("expected BadRequestError to propagate on config mismatch")


def test_ensure_stream_swallows_other_bad_request_errors():
    js = AsyncMock()
    js.add_stream.side_effect = BadRequestError(err_code=99999)

    asyncio.run(ensure_stream(js))  # must not raise
