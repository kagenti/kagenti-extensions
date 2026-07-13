import pytest
from starlette.testclient import TestClient

from scenario import TOOL_SCOPES


@pytest.fixture(scope="module")
def client():
    from server import app

    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


def _tools_list(client: TestClient) -> list[dict]:
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={"Accept": "application/json"},
    )
    assert response.status_code == 200
    return response.json()["result"]["tools"]


class TestToolsList:
    def test_returns_exactly_four_tool_names(self, client):
        tools = _tools_list(client)
        assert {t["name"] for t in tools} == set(TOOL_SCOPES.keys())

    def test_descriptions_match_scenario(self, client):
        tools = _tools_list(client)
        by_name = {t["name"]: t["description"] for t in tools}
        for name, expected in TOOL_SCOPES.items():
            assert by_name[name] == expected, f"description mismatch for {name!r}"

    def test_input_schema_is_minimal(self, client):
        tools = _tools_list(client)
        for tool in tools:
            schema = tool["inputSchema"]
            assert schema["type"] == "object"
            assert schema["properties"] == {}


class TestToolsCall:
    def test_valid_tool_returns_stub_content(self, client):
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "source-read", "arguments": {}},
            },
            headers={"Accept": "application/json"},
        )
        assert response.status_code == 200
        body = response.json()
        assert "result" in body
        assert body["result"]["content"]
