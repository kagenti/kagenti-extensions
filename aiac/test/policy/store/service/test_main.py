"""Unit tests for aiac/policy/store/service/main.py FastAPI application."""

import sqlite3
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import aiac.policy.store.service.main as svc
from aiac.idp.configuration.models import Role, Scope, Service, Subject
from aiac.policy.model.models import AgentPolicyModel, PolicyModel, PolicyRule
from aiac.policy.store.service.main import app, get_db


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def _role(id: str = "role-1", name: str = "admin") -> Role:
    return Role(id=id, name=name, composite=False)


def _scope(id: str = "scope-1", name: str = "read") -> Scope:
    return Scope(id=id, name=name)


def _service(id: str = "svc-1", service_id: str = "my-service") -> Service:
    return Service(id=id, serviceId=service_id, enabled=True)


def _subject(id: str = "sub-1", username: str = "alice") -> Subject:
    return Subject(id=id, username=username, enabled=True)


def _make_agent(agent_id: str = "agent-1") -> AgentPolicyModel:
    return AgentPolicyModel(
        agent_id=agent_id,
        agent_roles=[_role()],
        agent_scopes=[_scope()],
        subject_roles={_subject().id: [_role()]},
        source_roles={_service().id: [_role()]},
        target_scopes={_service().id: [_scope()]},
        inbound_rules=[PolicyRule(role=_role(), scope=_scope())],
        outbound_rules=[],
    )


@pytest.fixture
def client():
    """In-memory SQLite DB injected; lifespan bypassed."""
    conn = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
    svc._init_db(conn)
    svc._db_conn = conn
    svc._cache = {}
    app.dependency_overrides[get_db] = lambda: conn
    yield TestClient(app)
    app.dependency_overrides.clear()
    conn.close()
    svc._db_conn = None
    svc._cache = {}


@pytest.fixture
def client_with_agent(client):
    """Client with one pre-loaded agent in DB and cache."""
    agent = _make_agent("agent-1")
    conn = svc._db_conn
    conn.execute(
        "INSERT INTO agent_policies (agent_id, spec) VALUES (?, ?)",
        ("agent-1", agent.model_dump_json()),
    )
    svc._cache["agent-1"] = agent
    return client


# ---------------------------------------------------------------------------
# Startup: cache population
# ---------------------------------------------------------------------------


class TestStartup:
    def test_load_cache_populates_from_db_rows(self):
        conn = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
        svc._init_db(conn)
        agent = _make_agent("agent-1")
        conn.execute(
            "INSERT INTO agent_policies (agent_id, spec) VALUES (?, ?)",
            ("agent-1", agent.model_dump_json()),
        )

        svc._load_cache(conn)

        assert "agent-1" in svc._cache
        assert svc._cache["agent-1"].agent_id == "agent-1"
        conn.close()
        svc._cache = {}

    def test_load_cache_empty_when_db_empty(self):
        conn = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
        svc._init_db(conn)

        svc._load_cache(conn)

        assert svc._cache == {}
        conn.close()


# ---------------------------------------------------------------------------
# GET /policy
# ---------------------------------------------------------------------------


class TestGetPolicy:
    def test_returns_empty_policy_model_when_cache_empty(self, client):
        resp = client.get("/policy")
        assert resp.status_code == 200
        body = resp.json()
        assert body["agents"] == []

    def test_returns_policy_model_with_agents_from_cache(self, client_with_agent):
        resp = client_with_agent.get("/policy")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["agents"]) == 1
        assert body["agents"][0]["agent_id"] == "agent-1"


# ---------------------------------------------------------------------------
# GET /policy/agents/{agent_id}
# ---------------------------------------------------------------------------


class TestGetAgentPolicy:
    def test_returns_agent_policy_model_when_in_cache(self, client_with_agent):
        resp = client_with_agent.get("/policy/agents/agent-1")
        assert resp.status_code == 200
        assert resp.json()["agent_id"] == "agent-1"

    def test_returns_404_when_agent_not_in_cache(self, client):
        resp = client.get("/policy/agents/missing-agent")
        assert resp.status_code == 404
        assert resp.json() == {"error": "agent missing-agent not found"}

    def test_get_after_post_returns_updated_value_from_cache_not_db(self, client):
        agent = _make_agent("agent-x")
        client.post("/policy/agents/agent-x", json=agent.model_dump())
        resp = client.get("/policy/agents/agent-x")
        assert resp.status_code == 200
        assert resp.json()["agent_id"] == "agent-x"


# ---------------------------------------------------------------------------
# POST /policy/agents/{agent_id}
# ---------------------------------------------------------------------------


class TestUpsertAgentPolicy:
    def test_writes_to_db_updates_cache_returns_204(self, client):
        agent = _make_agent("agent-2")
        resp = client.post("/policy/agents/agent-2", json=agent.model_dump())
        assert resp.status_code == 204
        assert "agent-2" in svc._cache
        row = svc._db_conn.execute(
            "SELECT spec FROM agent_policies WHERE agent_id = ?", ("agent-2",)
        ).fetchone()
        assert row is not None

    def test_returns_502_on_sqlite_error(self, client):
        bad_conn = MagicMock()
        bad_conn.execute.side_effect = sqlite3.OperationalError("disk full")
        app.dependency_overrides[get_db] = lambda: bad_conn
        agent = _make_agent("agent-err")
        resp = client.post("/policy/agents/agent-err", json=agent.model_dump())
        assert resp.status_code == 502
        assert "error" in resp.json()


# ---------------------------------------------------------------------------
# POST /policy (full rebuild)
# ---------------------------------------------------------------------------


class TestReplacePolicy:
    def test_full_rebuild_replaces_cache_returns_204(self, client_with_agent):
        new_agent = _make_agent("agent-new")
        policy = PolicyModel(agents=[new_agent])
        resp = client_with_agent.post("/policy", json=policy.model_dump())
        assert resp.status_code == 204
        assert "agent-1" not in svc._cache
        assert "agent-new" in svc._cache

    def test_deletes_all_rows_and_inserts_new_rows(self, client_with_agent):
        new_agent = _make_agent("agent-new")
        policy = PolicyModel(agents=[new_agent])
        client_with_agent.post("/policy", json=policy.model_dump())
        rows = svc._db_conn.execute(
            "SELECT agent_id FROM agent_policies"
        ).fetchall()
        agent_ids = {r[0] for r in rows}
        assert agent_ids == {"agent-new"}

    def test_returns_502_on_sqlite_error(self, client):
        bad_conn = MagicMock()
        bad_conn.execute.side_effect = sqlite3.OperationalError("disk full")
        app.dependency_overrides[get_db] = lambda: bad_conn
        policy = PolicyModel(agents=[_make_agent()])
        resp = client.post("/policy", json=policy.model_dump())
        assert resp.status_code == 502
        assert "error" in resp.json()


# ---------------------------------------------------------------------------
# DELETE /policy/agents/{agent_id}
# ---------------------------------------------------------------------------


class TestDeleteAgentPolicy:
    def test_removes_row_from_db_and_cache_returns_204(self, client_with_agent):
        resp = client_with_agent.delete("/policy/agents/agent-1")
        assert resp.status_code == 204
        assert "agent-1" not in svc._cache
        row = svc._db_conn.execute(
            "SELECT agent_id FROM agent_policies WHERE agent_id = ?", ("agent-1",)
        ).fetchone()
        assert row is None

    def test_returns_502_on_sqlite_error(self, client):
        bad_conn = MagicMock()
        bad_conn.execute.side_effect = sqlite3.OperationalError("disk full")
        app.dependency_overrides[get_db] = lambda: bad_conn
        resp = client.delete("/policy/agents/agent-1")
        assert resp.status_code == 502
        assert "error" in resp.json()


# ---------------------------------------------------------------------------
# DELETE /policy
# ---------------------------------------------------------------------------


class TestDeletePolicy:
    def test_removes_all_rows_clears_cache_returns_204(self, client_with_agent):
        resp = client_with_agent.delete("/policy")
        assert resp.status_code == 204
        assert svc._cache == {}
        rows = svc._db_conn.execute("SELECT agent_id FROM agent_policies").fetchall()
        assert rows == []

    def test_returns_502_on_sqlite_error(self, client):
        bad_conn = MagicMock()
        bad_conn.execute.side_effect = sqlite3.OperationalError("disk full")
        app.dependency_overrides[get_db] = lambda: bad_conn
        resp = client.delete("/policy")
        assert resp.status_code == 502
        assert "error" in resp.json()


# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------


class TestHealth:
    def test_returns_200_when_sqlite_reachable(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_returns_503_when_sqlite_unavailable(self, client):
        bad_conn = MagicMock()
        bad_conn.execute.side_effect = sqlite3.OperationalError("disk I/O error")
        app.dependency_overrides[get_db] = lambda: bad_conn
        resp = client.get("/health")
        assert resp.status_code == 503
