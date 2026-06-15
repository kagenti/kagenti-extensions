"""Unit tests for aiac.agent.controller.nats_consumer (issue 4.12)."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_msg(subject: str, data: bytes = b"{}") -> MagicMock:
    msg = MagicMock()
    msg.subject = subject
    msg.data = data
    msg.ack = AsyncMock()
    return msg


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# subject dispatch
# ---------------------------------------------------------------------------


class TestSubjectDispatch:
    def test_policy_build_dispatches_to_policy_handler(self):
        from aiac.agent.controller.nats_consumer import _dispatch

        msg = _make_msg("aiac.apply.policy.build")
        policy_handler = AsyncMock(return_value=None)
        role_handler = AsyncMock()
        service_handler = AsyncMock()

        run(_dispatch(msg, policy_handler=policy_handler, role_handler=role_handler, service_handler=service_handler))

        policy_handler.assert_awaited_once()
        role_handler.assert_not_awaited()
        service_handler.assert_not_awaited()

    def test_role_subject_dispatches_to_role_handler_with_id(self):
        from aiac.agent.controller.nats_consumer import _dispatch

        msg = _make_msg("aiac.apply.role.role-uuid-1")
        role_handler = AsyncMock(return_value=None)

        run(_dispatch(msg, policy_handler=AsyncMock(), role_handler=role_handler, service_handler=AsyncMock()))

        role_handler.assert_awaited_once()
        call_kwargs = str(role_handler.call_args)
        assert "role-uuid-1" in call_kwargs

    def test_service_subject_dispatches_to_service_handler_with_id(self):
        from aiac.agent.controller.nats_consumer import _dispatch

        msg = _make_msg("aiac.apply.service.svc-1")
        service_handler = AsyncMock(return_value=None)

        run(_dispatch(msg, policy_handler=AsyncMock(), role_handler=AsyncMock(), service_handler=service_handler))

        service_handler.assert_awaited_once()
        call_kwargs = str(service_handler.call_args)
        assert "svc-1" in call_kwargs

    def test_unknown_subject_no_handler_invoked(self):
        from aiac.agent.controller.nats_consumer import _dispatch

        msg = _make_msg("aiac.apply.unknown.thing")
        policy_handler = AsyncMock()
        role_handler = AsyncMock()
        service_handler = AsyncMock()

        run(_dispatch(msg, policy_handler=policy_handler, role_handler=role_handler, service_handler=service_handler))

        policy_handler.assert_not_awaited()
        role_handler.assert_not_awaited()
        service_handler.assert_not_awaited()

    def test_policy_rebuild_subject_no_handler_invoked(self):
        from aiac.agent.controller.nats_consumer import _dispatch

        # rebuild is HTTP-only; NATS consumer must not dispatch it
        msg = _make_msg("aiac.apply.policy.rebuild")
        policy_handler = AsyncMock()
        role_handler = AsyncMock()
        service_handler = AsyncMock()

        run(_dispatch(msg, policy_handler=policy_handler, role_handler=role_handler, service_handler=service_handler))

        policy_handler.assert_not_awaited()
        role_handler.assert_not_awaited()
        service_handler.assert_not_awaited()


# ---------------------------------------------------------------------------
# ack / nack behaviour
# ---------------------------------------------------------------------------


class TestAckBehaviour:
    def test_successful_handler_issues_ack(self):
        from aiac.agent.controller.nats_consumer import _dispatch

        msg = _make_msg("aiac.apply.role.r1")
        role_handler = AsyncMock(return_value=None)

        run(_dispatch(msg, policy_handler=AsyncMock(), role_handler=role_handler, service_handler=AsyncMock()))

        msg.ack.assert_awaited_once()

    def test_handler_exception_no_ack(self):
        from aiac.agent.controller.nats_consumer import _dispatch

        msg = _make_msg("aiac.apply.role.r1")
        role_handler = AsyncMock(side_effect=Exception("handler failed"))

        run(_dispatch(msg, policy_handler=AsyncMock(), role_handler=role_handler, service_handler=AsyncMock()))

        msg.ack.assert_not_awaited()

    def test_ack_called_after_handler_completes(self):
        """Ack must NOT be issued before the handler returns (no fire-and-forget)."""
        from aiac.agent.controller.nats_consumer import _dispatch

        call_order = []
        msg = _make_msg("aiac.apply.role.r1")

        async def handler_recording(*args, **kwargs):
            call_order.append("handler")

        async def ack_recording():
            call_order.append("ack")

        msg.ack = ack_recording
        role_handler = AsyncMock(side_effect=handler_recording)

        run(_dispatch(msg, policy_handler=AsyncMock(), role_handler=role_handler, service_handler=AsyncMock()))

        assert call_order == ["handler", "ack"], f"Expected handler before ack, got: {call_order}"

    def test_unknown_subject_no_ack(self):
        from aiac.agent.controller.nats_consumer import _dispatch

        msg = _make_msg("aiac.apply.something.weird")

        run(_dispatch(msg, policy_handler=AsyncMock(), role_handler=AsyncMock(), service_handler=AsyncMock()))

        msg.ack.assert_not_awaited()


# ---------------------------------------------------------------------------
# consumer loop cancellation
# ---------------------------------------------------------------------------


def _make_nc_mock():
    """nc is a plain MagicMock: jetstream() is sync, drain() is async."""
    nc = MagicMock()
    nc.drain = AsyncMock()
    return nc


def _make_js_mock(sub_mock):
    """js is a plain MagicMock: subscribe() is async."""
    js = MagicMock()
    js.subscribe = AsyncMock(return_value=sub_mock)
    return js


def _make_sub_mock(side_effect=None):
    sub = MagicMock()
    sub.next_msg = AsyncMock(side_effect=side_effect)
    return sub


class TestConsumerLoop:
    def test_loop_exits_cleanly_on_cancellation(self):
        from aiac.agent.controller.nats_consumer import _nats_consumer_loop

        async def _run():
            mock_sub = _make_sub_mock(side_effect=asyncio.CancelledError())
            mock_nc = _make_nc_mock()
            mock_js = _make_js_mock(mock_sub)
            mock_nc.jetstream.return_value = mock_js

            with patch("aiac.agent.controller.nats_consumer.nats") as mock_nats_module:
                mock_nats_module.connect = AsyncMock(return_value=mock_nc)

                try:
                    await _nats_consumer_loop(
                        policy_handler=AsyncMock(),
                        role_handler=AsyncMock(),
                        service_handler=AsyncMock(),
                    )
                except asyncio.CancelledError:
                    pass  # loop re-raises CancelledError — acceptable

        run(_run())

    def test_nats_connection_failure_retries(self):
        from aiac.agent.controller.nats_consumer import _nats_consumer_loop

        async def _run():
            connect_calls = [0]

            with patch("aiac.agent.controller.nats_consumer.nats") as mock_nats_module:
                async def connect_side_effect(*args, **kwargs):
                    connect_calls[0] += 1
                    if connect_calls[0] == 1:
                        raise OSError("connection refused")
                    raise asyncio.CancelledError()

                mock_nats_module.connect = AsyncMock(side_effect=connect_side_effect)

                try:
                    await _nats_consumer_loop(
                        policy_handler=AsyncMock(),
                        role_handler=AsyncMock(),
                        service_handler=AsyncMock(),
                        retry_delay=0,
                    )
                except asyncio.CancelledError:
                    pass

            return connect_calls[0]

        calls = run(_run())
        assert calls >= 2, "Expected retry after connection failure"
