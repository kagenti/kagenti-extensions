"""Unit tests for aiac.agent.eventbus.consumer.

Covers subject-based dispatch and the ack/DLQ contract: ack on success,
no ack (implicit redelivery) on failure below MAX_DELIVER, and DLQ
publish + term() once MAX_DELIVER is reached.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aiac.agent.eventbus.consumer import AiacEventConsumer, _handle
from aiac.agent.eventbus.stream import DLQ_SUBJECT, MAX_DELIVER


def _fake_msg(subject: str, num_delivered: int = 1) -> MagicMock:
    msg = MagicMock()
    msg.subject = subject
    msg.data = b'{"id":"x"}'
    msg.metadata.num_delivered = num_delivered
    msg.ack = AsyncMock()
    msg.term = AsyncMock()
    return msg


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
    consumer._nc = AsyncMock()
    msg = _fake_msg("aiac.apply.service.svc-1", num_delivered=MAX_DELIVER - 1)

    with patch("aiac.agent.eventbus.consumer.onboard_service", side_effect=RuntimeError("boom")):
        asyncio.run(consumer._dispatch(msg))

    msg.ack.assert_not_called()
    msg.term.assert_not_called()
    consumer._nc.publish.assert_not_called()


def test_dispatch_routes_to_dlq_after_max_deliver():
    consumer = AiacEventConsumer()
    consumer._nc = AsyncMock()
    msg = _fake_msg("aiac.apply.service.svc-1", num_delivered=MAX_DELIVER)

    with patch("aiac.agent.eventbus.consumer.onboard_service", side_effect=RuntimeError("boom")):
        asyncio.run(consumer._dispatch(msg))

    msg.ack.assert_not_called()
    msg.term.assert_called_once()
    consumer._nc.publish.assert_called_once_with(DLQ_SUBJECT, msg.data)
