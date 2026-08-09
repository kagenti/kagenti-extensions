import os
import json
import pytest
from starlette.testclient import TestClient
from starlette.applications import Starlette
from a2a.server.routes import create_agent_card_routes

from a2a_agent import get_agent_card


@pytest.fixture
def card():
    return get_agent_card("localhost", 8000)


def test_card_name_and_version(card):
    assert card.name == "Github agent"
    assert card.version == "1.0.0"


def test_card_description(card):
    assert card.description == (
        "Autonomous Agent acting on a user's behalf against source repositories and an issue tracker. "
        "It inspects and changes repository source contents and reads, creates, and updates issues and their threads."
    )


def test_card_two_skills(card):
    assert len(card.skills) == 2
    skill_ids = {s.id for s in card.skills}
    assert skill_ids == {"source_operations", "issue_operations"}


def test_skill_tags_and_examples(card):
    skills_by_id = {s.id: s for s in card.skills}

    source = skills_by_id["source_operations"]
    assert "github" in source.tags
    assert len(source.examples) >= 1

    issue = skills_by_id["issue_operations"]
    assert "github" in issue.tags
    assert len(issue.examples) >= 1


def test_bearer_security_scheme(card):
    assert "Bearer" in card.security_schemes
    assert card.security_schemes["Bearer"].http_auth_security_scheme.scheme == "bearer"


def test_agent_interface_jsonrpc(card):
    assert card.supported_interfaces[0].protocol_binding == "JSONRPC"


def test_agent_endpoint_override():
    import a2a_agent

    original = a2a_agent.settings.AGENT_ENDPOINT
    a2a_agent.settings.AGENT_ENDPOINT = "http://custom.host:9999"
    try:
        card = get_agent_card("localhost", 8000)
        assert card.supported_interfaces[0].url == "http://custom.host:9999/"
    finally:
        a2a_agent.settings.AGENT_ENDPOINT = original


def test_capabilities_streaming(card):
    assert card.capabilities.streaming is True


def test_input_output_modes(card):
    assert list(card.default_input_modes) == ["text"]
    assert list(card.default_output_modes) == ["text"]


def test_well_known_agent_card_route(card):
    routes = create_agent_card_routes(card)
    app = Starlette(routes=routes)
    client = TestClient(app)
    response = client.get("/.well-known/agent-card.json")
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "Github agent"
    assert len(data["skills"]) == 2


def test_well_known_agent_json_route(card):
    routes = create_agent_card_routes(card, card_url="/.well-known/agent.json")
    app = Starlette(routes=routes)
    client = TestClient(app)
    response = client.get("/.well-known/agent.json")
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "Github agent"
    assert len(data["skills"]) == 2
