"""Unit tests for aiac.agent.eventbus.consumer.

Covers subject-based dispatch and the ack/DLQ contract: ack on success,
no ack (implicit redelivery) on failure below MAX_DELIVER, and DLQ
publish + term() once MAX_DELIVER is reached.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from nats.js.api import AckPolicy

from aiac.agent.eventbus.consumer import AiacEventConsumer, _handle, lifespan
from aiac.agent.eventbus.stream import (
    ACK_WAIT_SECONDS,
    CONSUMER_FILTER_SUBJECTS,
    CONSUMER_NAME,
    DLQ_SUBJECT,
    MAX_DELIVER,
    STREAM_NAME,
)


def _fake_msg(subject: str, num_delivered: int = 1) -> MagicMock:
    msg = MagicMock()
    msg.subject = subject
    msg.data = b'{"id":"x"}'
    msg.metadata.num_delivered = num_delivered
    msg.ack = AsyncMock()
    msg.term = AsyncMock()
    return msg


def _fake_nc() -> MagicMock:
    # .jetstream() is sync in the real client and returns a context whose .publish() is
    # async — a plain AsyncMock would make jetstream() itself async, unlike the real client.
    nc = MagicMock()
    nc.jetstream.return_value = AsyncMock()
    return nc


def test_handle_routes_service_subject_to_onboard_service():
    with patch("aiac.agent.eventbus.consumer.onboard_service", return_value=([], False)) as onboard:
        result = _handle("aiac.apply.service.svc-1")

    onboard.assert_called_once_with("svc-1")
    assert result == ([], False)


def test_handle_routes_role_subject_to_update_role():
    with patch("aiac.agent.eventbus.consumer.update_role", return_value=([], True)) as role:
        result = _handle("aiac.apply.role.role-1")

    role.assert_called_once_with("role-1")
    assert result == ([], True)


def test_handle_decodes_percent_encoded_dotted_role_name():
    # Mirrors SubjectMapper.encodeSubjectToken on the Keycloak SPI side: a role name
    # containing '.' (e.g. "team.admin") is percent-encoded into one NATS subject token.
    with patch("aiac.agent.eventbus.consumer.update_role", return_value=([], True)) as role:
        result = _handle("aiac.apply.role.team%2Eadmin")

    role.assert_called_once_with("team.admin")
    assert result == ([], True)


def test_handle_routes_policy_build_subject():
    with patch("aiac.agent.eventbus.consumer.build_policy", return_value=([], False)) as build:
        result = _handle("aiac.apply.policy.build")

    build.assert_called_once_with()
    assert result == ([], False)


def test_handle_raises_for_unknown_subject():
    with pytest.raises(ValueError):
        _handle("aiac.apply.offboard.svc-1")


def test_dispatch_acks_on_success():
    consumer = AiacEventConsumer()
    consumer._nc = AsyncMock()
    msg = _fake_msg("aiac.apply.service.svc-1")

    with (
        patch("aiac.agent.eventbus.consumer.onboard_service", return_value=([], False)),
        patch("aiac.agent.eventbus.consumer.compute_and_apply") as pce,
    ):
        asyncio.run(consumer._dispatch(msg))

    pce.assert_called_once_with([], False)
    msg.ack.assert_called_once()
    msg.term.assert_not_called()


def test_dispatch_leaves_message_unacked_before_max_deliver():
    consumer = AiacEventConsumer()
    consumer._nc = _fake_nc()
    msg = _fake_msg("aiac.apply.service.svc-1", num_delivered=MAX_DELIVER - 1)

    with patch("aiac.agent.eventbus.consumer.onboard_service", side_effect=RuntimeError("boom")):
        asyncio.run(consumer._dispatch(msg))

    msg.ack.assert_not_called()
    msg.term.assert_not_called()
    consumer._nc.jetstream.assert_not_called()


def test_dispatch_routes_to_dlq_after_max_deliver():
    consumer = AiacEventConsumer()
    consumer._nc = _fake_nc()
    msg = _fake_msg("aiac.apply.service.svc-1", num_delivered=MAX_DELIVER)

    with patch("aiac.agent.eventbus.consumer.onboard_service", side_effect=RuntimeError("boom")):
        asyncio.run(consumer._dispatch(msg))

    msg.ack.assert_not_called()
    msg.term.assert_called_once()
    consumer._nc.jetstream.return_value.publish.assert_called_once_with(DLQ_SUBJECT, msg.data)


def test_start_with_retry_retries_on_failure_then_succeeds():
    consumer = AiacEventConsumer()
    attempts = AsyncMock(side_effect=[RuntimeError("connect failed"), RuntimeError("connect failed"), None])

    with (
        patch.object(consumer, "start", attempts),
        patch("aiac.agent.eventbus.consumer.asyncio.sleep", new=AsyncMock()) as sleep,
    ):
        asyncio.run(consumer.start_with_retry())

    assert attempts.call_count == 3
    assert sleep.call_count == 2


def test_start_connects_and_subscribes_with_expected_config():
    consumer = AiacEventConsumer()
    fake_js = AsyncMock()
    fake_nc = MagicMock()
    fake_nc.jetstream.return_value = fake_js

    with (
        patch("aiac.agent.eventbus.consumer.nats.connect", AsyncMock(return_value=fake_nc)) as connect,
        patch("aiac.agent.eventbus.consumer.ensure_stream", AsyncMock()) as ensure_stream_mock,
    ):
        asyncio.run(consumer.start())

    connect.assert_called_once_with(consumer._nats_url, max_reconnect_attempts=-1)
    ensure_stream_mock.assert_called_once_with(fake_js)
    _, kwargs = fake_js.subscribe.call_args
    assert kwargs["subject"] == "aiac.apply.>"
    assert kwargs["queue"] == CONSUMER_NAME
    assert kwargs["durable"] == CONSUMER_NAME
    assert kwargs["stream"] == STREAM_NAME
    assert kwargs["manual_ack"] is True
    assert kwargs["cb"] == consumer._dispatch
    config = kwargs["config"]
    assert config.filter_subjects == CONSUMER_FILTER_SUBJECTS
    assert config.ack_policy == AckPolicy.EXPLICIT
    assert config.max_deliver == MAX_DELIVER
    assert config.ack_wait == ACK_WAIT_SECONDS
    assert consumer._nc is fake_nc
    assert consumer._sub is fake_js.subscribe.return_value


def test_lifespan_awaits_cancelled_task_before_stopping_consumer():
    # Regression test for the shutdown race: task.cancel() alone doesn't wait for the
    # task to actually finish, so consumer.stop() could previously run while
    # start_with_retry (or _dispatch, mid-message) was still executing.
    events = []

    class FakeConsumer:
        async def start_with_retry(self):
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                events.append("task_cancelled")
                raise

        async def stop(self):
            events.append("stopped")

    async def run():
        with patch("aiac.agent.eventbus.consumer.AiacEventConsumer", return_value=FakeConsumer()):
            async with lifespan(MagicMock()):
                await asyncio.sleep(0)  # let the background task actually start

    asyncio.run(run())

    assert events == ["task_cancelled", "stopped"]


def test_dispatch_does_not_term_when_dlq_publish_fails():
    consumer = AiacEventConsumer()
    consumer._nc = _fake_nc()
    consumer._nc.jetstream.return_value.publish.side_effect = RuntimeError("dlq publish failed")
    msg = _fake_msg("aiac.apply.service.svc-1", num_delivered=MAX_DELIVER)

    with (
        patch("aiac.agent.eventbus.consumer.onboard_service", side_effect=RuntimeError("boom")),
        pytest.raises(RuntimeError, match="dlq publish failed"),
    ):
        asyncio.run(consumer._dispatch(msg))

    msg.ack.assert_not_called()
    msg.term.assert_not_called()
