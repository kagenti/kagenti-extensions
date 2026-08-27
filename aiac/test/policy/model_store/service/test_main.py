"""Unit tests for aiac/policy/model_store/service/main.py FastAPI application.

SPM-centric surface: the store persists ``ServicePolicyModel`` rows keyed by
``service_id``. ``AgentPolicyModel`` is derived and never persisted, so there are no
per-agent or whole-collection endpoints. Seam: an in-memory SQLite connection is injected
on startup instead of opening ``SERVICEPOLICY_DB_PATH``.
"""

import sqlite3
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import aiac.policy.model_store.service.main as svc
from aiac.idp.configuration.models import Role, Scope, ServiceType
from aiac.policy.model.models import PolicyRule, ServicePolicyModel
from aiac.policy.model_store.keying import encode_service_id
from aiac.policy.model_store.service.main import app, get_db

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def _role(id: str = "role-1", name: str = "admin") -> Role:
    return Role(id=id, name=name, composite=False)


def _scope(id: str = "scope-1", name: str = "read", service_id: str = "my-service") -> Scope:
    return Scope(id=id, name=name, serviceId=service_id)


def _spm(
    service_id: str = "my-service",
    role_id: str = "role-1",
    service_type: ServiceType = ServiceType.AGENT,
) -> ServicePolicyModel:
    return ServicePolicyModel(
        service_id=service_id,
        service_type=service_type,
        owned_roles=[_role()],
        owned_scopes=[_scope(service_id=service_id)],
        inbound_allow_rules=[PolicyRule(role=_role(id=role_id), scope=_scope(service_id=service_id))],
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


def _preload(spm: ServicePolicyModel) -> None:
    """Insert an SPM directly into the current DB + cache (simulating prior state)."""
    svc._db_conn.execute(
        "INSERT OR REPLACE INTO service_policies (service_id, spec) VALUES (?, ?)",
        (spm.service_id, spm.model_dump_json()),
    )
    svc._cache[spm.service_id] = spm


# ---------------------------------------------------------------------------
# Startup: cache population from SQLite
# ---------------------------------------------------------------------------


class TestStartup:
    def test_load_cache_populates_from_db_rows(self):
        conn = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
        svc._init_db(conn)
        spm = _spm("weather-service")
        conn.execute(
            "INSERT INTO service_policies (service_id, spec) VALUES (?, ?)",
            ("weather-service", spm.model_dump_json()),
        )

        svc._load_cache(conn)

        assert "weather-service" in svc._cache
        assert svc._cache["weather-service"].service_id == "weather-service"
        conn.close()
        svc._cache = {}

    def test_load_cache_empty_when_db_empty(self):
        conn = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
        svc._init_db(conn)

        svc._load_cache(conn)

        assert svc._cache == {}
        conn.close()
        svc._cache = {}


# ---------------------------------------------------------------------------
# GET /policy/services/{service_id}  (by-id)
# ---------------------------------------------------------------------------


class TestGetServicePolicy:
    def test_returns_spm_when_in_cache(self, client):
        _preload(_spm("weather-service"))
        resp = client.get(f"/policy/services/{encode_service_id('weather-service')}")
        assert resp.status_code == 200
        assert resp.json()["service_id"] == "weather-service"

    def test_returns_404_when_not_in_cache(self, client):
        resp = client.get(f"/policy/services/{encode_service_id('missing-service')}")
        assert resp.status_code == 404
        assert resp.json() == {"error": "service missing-service not found"}

    def test_get_after_post_returns_updated_value_from_cache(self, client):
        client.post(f"/policy/services/{encode_service_id('svc-x')}", json=_spm("svc-x").model_dump())
        resp = client.get(f"/policy/services/{encode_service_id('svc-x')}")
        assert resp.status_code == 200
        assert resp.json()["service_id"] == "svc-x"

    def test_slash_bearing_id_round_trips_via_encoded_path(self, client):
        service_id = "team1/github-agent"
        client.post(f"/policy/services/{encode_service_id(service_id)}", json=_spm(service_id).model_dump())
        resp = client.get(f"/policy/services/{encode_service_id(service_id)}")
        assert resp.status_code == 200
        assert resp.json()["service_id"] == service_id
        assert service_id in svc._cache
        row = svc._db_conn.execute(
            "SELECT service_id FROM service_policies WHERE service_id = ?", (service_id,)
        ).fetchone()
        assert row is not None


# ---------------------------------------------------------------------------
# GET /policy/services?role={role_id}  (by-role)
# ---------------------------------------------------------------------------


class TestGetServicePoliciesByRole:
    def test_returns_single_spm_referencing_role(self, client):
        _preload(_spm("svc-a", role_id="user-role"))
        _preload(_spm("svc-b", role_id="other-role"))
        resp = client.get("/policy/services", params={"role": "user-role"})
        assert resp.status_code == 200
        body = resp.json()
        assert [s["service_id"] for s in body] == ["svc-a"]

    def test_returns_spm_when_role_referenced_only_in_deny_rules(self, client):
        # The scan must cover BOTH parallel lists: a role reference living only in
        # inbound_deny_rules (with an empty allow list) must still surface here, because
        # override-purge needs to find stale deny edges just as much as allow edges.
        deny_spm = ServicePolicyModel(
            service_id="svc-deny",
            service_type=ServiceType.AGENT,
            owned_roles=[_role()],
            owned_scopes=[_scope(service_id="svc-deny")],
            inbound_allow_rules=[],
            inbound_deny_rules=[PolicyRule(role=_role(id="denied-role"), scope=_scope(service_id="svc-deny"))],
        )
        _preload(deny_spm)
        resp = client.get("/policy/services", params={"role": "denied-role"})
        assert resp.status_code == 200
        assert [s["service_id"] for s in resp.json()] == ["svc-deny"]

    def test_returns_all_spms_when_several_reference_role(self, client):
        _preload(_spm("svc-a", role_id="shared-role"))
        _preload(_spm("svc-b", role_id="shared-role"))
        _preload(_spm("svc-c", role_id="other-role"))
        resp = client.get("/policy/services", params={"role": "shared-role"})
        assert resp.status_code == 200
        ids = sorted(s["service_id"] for s in resp.json())
        assert ids == ["svc-a", "svc-b"]

    def test_returns_empty_list_when_no_spm_references_role(self, client):
        _preload(_spm("svc-a", role_id="user-role"))
        resp = client.get("/policy/services", params={"role": "nobody"})
        assert resp.status_code == 200
        assert resp.json() == []

    def test_returns_empty_list_when_cache_empty(self, client):
        resp = client.get("/policy/services", params={"role": "anything"})
        assert resp.status_code == 200
        assert resp.json() == []


# ---------------------------------------------------------------------------
# POST /policy/services/{service_id}  (upsert)
# ---------------------------------------------------------------------------


class TestUpsertServicePolicy:
    def test_writes_to_db_updates_cache_returns_204(self, client):
        resp = client.post(f"/policy/services/{encode_service_id('svc-1')}", json=_spm("svc-1").model_dump())
        assert resp.status_code == 204
        assert "svc-1" in svc._cache
        row = svc._db_conn.execute("SELECT spec FROM service_policies WHERE service_id = ?", ("svc-1",)).fetchone()
        assert row is not None

    def test_repeat_post_replaces_row_upsert_round_trip(self, client):
        encoded = encode_service_id("svc-1")
        client.post(f"/policy/services/{encoded}", json=_spm("svc-1", role_id="role-a").model_dump())
        client.post(f"/policy/services/{encoded}", json=_spm("svc-1", role_id="role-b").model_dump())

        rows = svc._db_conn.execute(
            "SELECT service_id FROM service_policies WHERE service_id = ?", ("svc-1",)
        ).fetchall()
        assert len(rows) == 1  # replaced, not duplicated

        # The stored/cached SPM now carries the second write's rule.
        resp = client.get(f"/policy/services/{encoded}")
        rule_role_ids = [r["role"]["id"] for r in resp.json()["inbound_allow_rules"]]
        assert rule_role_ids == ["role-b"]

    def test_returns_502_on_sqlite_error(self, client):
        bad_conn = MagicMock()
        bad_conn.execute.side_effect = sqlite3.OperationalError("disk full")
        app.dependency_overrides[get_db] = lambda: bad_conn
        resp = client.post(f"/policy/services/{encode_service_id('svc-err')}", json=_spm("svc-err").model_dump())
        assert resp.status_code == 502
        assert "error" in resp.json()


# ---------------------------------------------------------------------------
# DELETE /policy/services/{service_id}
# ---------------------------------------------------------------------------


class TestDeleteServicePolicy:
    def test_removes_row_from_db_and_cache_returns_204(self, client):
        _preload(_spm("svc-1"))
        resp = client.delete(f"/policy/services/{encode_service_id('svc-1')}")
        assert resp.status_code == 204
        assert "svc-1" not in svc._cache
        row = svc._db_conn.execute(
            "SELECT service_id FROM service_policies WHERE service_id = ?", ("svc-1",)
        ).fetchone()
        assert row is None

    def test_returns_502_on_sqlite_error(self, client):
        bad_conn = MagicMock()
        bad_conn.execute.side_effect = sqlite3.OperationalError("disk full")
        app.dependency_overrides[get_db] = lambda: bad_conn
        resp = client.delete(f"/policy/services/{encode_service_id('svc-1')}")
        assert resp.status_code == 502
        assert "error" in resp.json()


# ---------------------------------------------------------------------------
# DELETE /policy/services  (clear-all)
# ---------------------------------------------------------------------------


class TestClearServicePolicies:
    def test_clears_all_rows_from_db_and_cache_returns_204(self, client):
        _preload(_spm("svc-a"))
        _preload(_spm("svc-b"))
        resp = client.delete("/policy/services")
        assert resp.status_code == 204
        assert svc._cache == {}
        rows = svc._db_conn.execute("SELECT service_id FROM service_policies").fetchall()
        assert rows == []

    def test_clear_empty_store_is_noop_204(self, client):
        resp = client.delete("/policy/services")
        assert resp.status_code == 204
        assert svc._cache == {}

    def test_clear_then_get_by_id_404s(self, client):
        _preload(_spm("svc-a"))
        client.delete("/policy/services")
        resp = client.get(f"/policy/services/{encode_service_id('svc-a')}")
        assert resp.status_code == 404

    def test_returns_502_on_sqlite_error(self, client):
        bad_conn = MagicMock()
        bad_conn.execute.side_effect = sqlite3.OperationalError("disk full")
        app.dependency_overrides[get_db] = lambda: bad_conn
        resp = client.delete("/policy/services")
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


# ---------------------------------------------------------------------------
# Removed surface: no per-agent / whole-collection endpoints
# ---------------------------------------------------------------------------


class TestRemovedEndpoints:
    def test_whole_collection_get_policy_absent(self, client):
        assert client.get("/policy").status_code == 404

    def test_per_agent_get_absent(self, client):
        assert client.get("/policy/agents/agent-1").status_code == 404
