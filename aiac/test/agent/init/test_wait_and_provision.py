"""Unit tests for aiac.agent.init.wait_and_provision (the aiac-init entrypoint).

The retry-wrapped wait helpers are called via ``.__wrapped__`` to exercise the
underlying check without engaging tenacity's real retry/backoff loop. ``main``
is tested for the AIAC_RAG_INGEST_URL-optional behavior, which is the one
deliberate deviation from the literal spec (see stream.py / event-broker.md).
"""

from unittest.mock import AsyncMock, patch

from aiac.agent.init import wait_and_provision as wp


def test_wait_for_tcp_connects_to_host_and_port():
    with patch("aiac.agent.init.wait_and_provision.socket.create_connection") as conn:
        wp._wait_for_tcp.__wrapped__("nats-host", 4222)

    conn.assert_called_once_with(("nats-host", 4222), timeout=2)


def test_wait_for_http_health_checks_health_endpoint():
    with patch("aiac.agent.init.wait_and_provision.httpx.get") as get:
        wp._wait_for_http_health.__wrapped__("http://svc:7071")

    get.assert_called_once_with("http://svc:7071/health", timeout=2)
    get.return_value.raise_for_status.assert_called_once()


def test_main_skips_rag_check_when_url_unset(monkeypatch):
    monkeypatch.setenv("AIAC_PDP_CONFIG_URL", "http://cfg")
    monkeypatch.setenv("AIAC_PDP_POLICY_URL", "http://policy")
    monkeypatch.delenv("AIAC_RAG_INGEST_URL", raising=False)

    with (
        patch("aiac.agent.init.wait_and_provision._wait_for_tcp") as tcp,
        patch("aiac.agent.init.wait_and_provision._wait_for_http_health") as health,
        patch("aiac.agent.init.wait_and_provision._provision_stream", new_callable=AsyncMock) as provision,
    ):
        wp.main()

    tcp.assert_called_once()
    assert health.call_count == 2  # IdP Config + PDP Policy only, no RAG Ingest
    provision.assert_called_once()


def test_main_checks_rag_when_url_set(monkeypatch):
    monkeypatch.setenv("AIAC_PDP_CONFIG_URL", "http://cfg")
    monkeypatch.setenv("AIAC_PDP_POLICY_URL", "http://policy")
    monkeypatch.setenv("AIAC_RAG_INGEST_URL", "http://rag")

    with (
        patch("aiac.agent.init.wait_and_provision._wait_for_tcp"),
        patch("aiac.agent.init.wait_and_provision._wait_for_http_health") as health,
        patch("aiac.agent.init.wait_and_provision._provision_stream", new_callable=AsyncMock),
    ):
        wp.main()

    assert health.call_count == 3
    health.assert_any_call("http://rag")
