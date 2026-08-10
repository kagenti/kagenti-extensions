"""Unit tests for aiac/pdp/service/policy/opa/main.py FastAPI application."""

from pathlib import Path

from fastapi.testclient import TestClient

from aiac.pdp.service.policy.opa.main import app, get_output_dir


def _make_client(out_dir: Path) -> TestClient:
    app.dependency_overrides[get_output_dir] = lambda: out_dir
    return TestClient(app)


def _agent(agent_id: str = "weather-agent") -> dict:
    return {
        "agent_id": agent_id,
        "agent_roles": [],
        "agent_scopes": [],
        "subject_roles": {},
        "source_roles": {},
        "target_scopes": {},
        "inbound_rules": [],
        "outbound_rules": [],
    }


# ---------------------------------------------------------------------------
# POST /policy -> 204, writes both files for every agent
# ---------------------------------------------------------------------------


class TestUpsertPolicy:
    def test_writes_files_for_every_agent_and_returns_204(self, tmp_path):
        body = {"agents": [_agent("weather-agent"), _agent("github-tool")]}
        resp = _make_client(tmp_path).post("/policy", json=body)
        assert resp.status_code == 204
        assert (tmp_path / "weather_agent.inbound.rego").exists()
        assert (tmp_path / "weather_agent.outbound.rego").exists()
        assert (tmp_path / "github_tool.inbound.rego").exists()
        assert (tmp_path / "github_tool.outbound.rego").exists()

    def teardown_method(self):
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# POST /policy/agents/{agent_id} -> 204, writes both .rego files
# ---------------------------------------------------------------------------


class TestUpsertAgent:
    def test_writes_both_rego_files_and_returns_204(self, tmp_path):
        resp = _make_client(tmp_path).post(
            "/policy/agents/weather-agent", json=_agent("weather-agent")
        )
        assert resp.status_code == 204
        assert (tmp_path / "weather_agent.inbound.rego").exists()
        assert (tmp_path / "weather_agent.outbound.rego").exists()

    def test_written_files_contain_generated_rego(self, tmp_path):
        _make_client(tmp_path).post(
            "/policy/agents/weather-agent", json=_agent("weather-agent")
        )
        inbound = (tmp_path / "weather_agent.inbound.rego").read_text()
        outbound = (tmp_path / "weather_agent.outbound.rego").read_text()
        assert "package authz.weather_agent.inbound" in inbound
        assert "package authz.weather_agent.outbound" in outbound

    def teardown_method(self):
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# DELETE /policy/agents/{agent_id} -> 204, removes both files
# ---------------------------------------------------------------------------


class TestDeleteAgent:
    def test_removes_both_files_and_returns_204(self, tmp_path):
        client = _make_client(tmp_path)
        client.post("/policy/agents/weather-agent", json=_agent("weather-agent"))
        resp = client.delete("/policy/agents/weather-agent")
        assert resp.status_code == 204
        assert not (tmp_path / "weather_agent.inbound.rego").exists()
        assert not (tmp_path / "weather_agent.outbound.rego").exists()

    def test_absent_files_still_returns_204(self, tmp_path):
        resp = _make_client(tmp_path).delete("/policy/agents/never-written")
        assert resp.status_code == 204

    def teardown_method(self):
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# DELETE /policy -> 204, removes all *.rego files
# ---------------------------------------------------------------------------


class TestDeleteAll:
    def test_removes_all_rego_files_and_returns_204(self, tmp_path):
        client = _make_client(tmp_path)
        client.post("/policy/agents/weather-agent", json=_agent("weather-agent"))
        client.post("/policy/agents/github-tool", json=_agent("github-tool"))
        resp = client.delete("/policy")
        assert resp.status_code == 204
        assert list(tmp_path.glob("*.rego")) == []

    def test_leaves_non_rego_files_untouched(self, tmp_path):
        (tmp_path / "keep.txt").write_text("data")
        resp = _make_client(tmp_path).delete("/policy")
        assert resp.status_code == 204
        assert (tmp_path / "keep.txt").exists()

    def teardown_method(self):
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Write endpoints -> 502 on OSError
# ---------------------------------------------------------------------------


class TestOSErrorHandling:
    def test_upsert_agent_returns_502_on_os_error(self, tmp_path):
        # out_dir points at a regular file, so writing a child path raises OSError
        not_a_dir = tmp_path / "afile"
        not_a_dir.write_text("x")
        resp = _make_client(not_a_dir).post(
            "/policy/agents/weather-agent", json=_agent("weather-agent")
        )
        assert resp.status_code == 502
        assert "error" in resp.json()

    def teardown_method(self):
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# GET /health -> 200 when dir writable, 503 otherwise
# ---------------------------------------------------------------------------


class TestHealth:
    # /health resolves the output dir from server config (REGO_OUTPUT_DIR) directly rather than
    # via the injectable dependency, so drive it through the env var instead of dependency_overrides.
    def test_returns_200_when_dir_writable(self, tmp_path, monkeypatch):
        monkeypatch.setenv("REGO_OUTPUT_DIR", str(tmp_path))
        resp = TestClient(app).get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_returns_503_when_dir_absent(self, tmp_path, monkeypatch):
        missing = tmp_path / "does-not-exist"
        monkeypatch.setenv("REGO_OUTPUT_DIR", str(missing))
        resp = TestClient(app).get("/health")
        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "unavailable"
        assert "error" in body

    def teardown_method(self):
        app.dependency_overrides.clear()
