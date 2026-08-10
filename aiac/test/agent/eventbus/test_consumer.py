"""Unit tests for aiac.agent.eventbus.consumer.

Covers subject-based dispatch and the ack/DLQ contract: ack on success,
no ack (implicit redelivery) on failure below MAX_DELIVER, and DLQ
publish + term() once MAX_DELIVER is reached.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aiac.agent.eventbus.consumer import AiacEventConsumer, _handle, lifespan
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


def test_handle_raises_for_policy_rebuild_subject():
    # policy.rebuild is not a routed subject — only policy.build is.
    with pytest.raises(ValueError):
        _handle("aiac.apply.policy.rebuild")


def test_handle_routes_dotted_role_name():
    # A Keycloak role name containing dots must survive intact through the subject
    # (guards the aiac.apply.role.> filter — see stream.CONSUMER_FILTER_SUBJECTS).
    with patch("aiac.agent.eventbus.consumer.update_role", return_value=([], True)) as role:
        result = _handle("aiac.apply.role.foo.bar")

    role.assert_called_once_with("foo.bar")
    assert result == ([], True)


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


def test_dispatch_unknown_subject_does_not_ack_term_or_dlq():
    # An unknown subject raises inside _handle; below MAX_DELIVER this is treated
    # like any handler failure — no ack, no term, no DLQ — so NATS redelivers.
    consumer = AiacEventConsumer()
    consumer._nc = AsyncMock()
    msg = _fake_msg("aiac.apply.offboard.svc-1", num_delivered=1)

    asyncio.run(consumer._dispatch(msg))

    msg.ack.assert_not_called()
    msg.term.assert_not_called()
    consumer._nc.publish.assert_not_called()


def test_dispatch_awaits_handler_before_ack():
    # Pins the "no premature ack / no fire-and-forget" contract: both the handler
    # and the PCE must complete before ack is issued, and ack fires exactly once.
    consumer = AiacEventConsumer()
    consumer._nc = AsyncMock()
    msg = _fake_msg("aiac.apply.service.svc-1")
    calls = []

    def record_handler(svc_id):
        assert not msg.ack.called  # ack must not have fired before the handler runs
        calls.append("handler")
        return ([], False)

    def record_pce(rules, override):
        assert not msg.ack.called  # ack must not have fired before the PCE runs
        calls.append("pce")

    with (
        patch("aiac.agent.eventbus.consumer.onboard_service", side_effect=record_handler),
        patch("aiac.agent.eventbus.consumer.compute_and_apply", side_effect=record_pce),
    ):
        asyncio.run(consumer._dispatch(msg))

    assert calls == ["handler", "pce"]
    msg.ack.assert_called_once()


def _fake_nats_client():
    """AsyncMock NATS client whose jetstream()/subscribe() drive start() to a
    subscription without a live broker."""
    nc = AsyncMock()
    js = MagicMock()
    sub = AsyncMock()
    nc.jetstream = MagicMock(return_value=js)  # jetstream() is sync in nats-py
    js.add_stream = AsyncMock()
    js.subscribe = AsyncMock(return_value=sub)
    return nc, js, sub


def test_lifespan_cancels_cleanly_and_stops():
    nc, _js, sub = _fake_nats_client()

    async def scenario():
        with patch("aiac.agent.eventbus.consumer.nats.connect", AsyncMock(return_value=nc)):
            async with lifespan(app=MagicMock()):
                # Let the backgrounded start() reach its subscription (all mocked
                # awaits resolve immediately; a short sleep drains them).
                await asyncio.sleep(0.05)
        # On context exit the task is cancelled, awaited (no uncaught exception),
        # and stop() unsubscribes + closes.
        sub.unsubscribe.assert_awaited_once()
        nc.close.assert_awaited_once()

    asyncio.run(scenario())


def test_start_retries_connect_with_backoff_until_success(monkeypatch):
    # Zero the backoff so retries are instant.
    monkeypatch.setattr("aiac.agent.eventbus.consumer.CONNECT_BACKOFF_MULTIPLIER", 0)
    monkeypatch.setattr("aiac.agent.eventbus.consumer.CONNECT_BACKOFF_MIN", 0)
    monkeypatch.setattr("aiac.agent.eventbus.consumer.CONNECT_BACKOFF_MAX", 0)

    nc, js, sub = _fake_nats_client()
    connect = AsyncMock(side_effect=[ConnectionError("down"), ConnectionError("down"), nc])

    with patch("aiac.agent.eventbus.consumer.nats.connect", connect):
        consumer = AiacEventConsumer()
        asyncio.run(consumer.start())

    assert connect.await_count == 3  # two transient failures, then success
    js.subscribe.assert_awaited_once()
    assert consumer._sub is sub
