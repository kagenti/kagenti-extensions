import pytest
from unittest.mock import AsyncMock, MagicMock

from github_agent.data_types import GithubQueryInfo
from github_agent.main import GithubAgent, _parse_prereq_from_raw
from github_agent.config import Settings


def make_agent(prereq_raw="", researcher_raw="done"):
    config = Settings()  # type: ignore
    agent = GithubAgent(config=config)
    agent.agents.prereq_identifier_task = MagicMock()
    agent.agents.prereq_identifier_task.output = MagicMock(raw=prereq_raw)
    agent.agents.prereq_id_crew = MagicMock()
    agent.agents.prereq_id_crew.kickoff_async = AsyncMock(return_value=None)
    agent.agents.github_query_task = MagicMock()
    agent.agents.github_query_task.output = MagicMock(raw=researcher_raw)
    agent.agents.crew = MagicMock()
    agent.agents.crew.kickoff_async = AsyncMock(return_value=None)
    return agent


def test_github_query_info_parses_flat_json():
    info = GithubQueryInfo(owner="kagenti", repo="my-repo", ref="main", path="README.md", numbers=[1, 2])
    assert info.owner == "kagenti"
    assert info.repo == "my-repo"
    assert info.ref == "main"
    assert info.path == "README.md"
    assert info.numbers == [1, 2]


def test_numbers_string_coercion():
    info = GithubQueryInfo(numbers="[1, 2]")
    assert info.numbers == [1, 2]


def test_parse_prereq_from_raw_valid_json():
    raw = '{"owner": "foo", "repo": "bar", "ref": null, "path": null, "numbers": null}'
    result = _parse_prereq_from_raw(raw)
    assert result.owner == "foo"
    assert result.repo == "bar"
    assert result.ref is None
    assert result.numbers is None


def test_parse_prereq_from_raw_unparseable():
    result = _parse_prereq_from_raw("not json at all")
    assert result.owner is None
    assert result.repo is None
    assert result.ref is None
    assert result.path is None
    assert result.numbers is None


@pytest.mark.anyio
async def test_gate_numbers_without_owner_repo():
    prereq_raw = '{"owner": null, "repo": null, "ref": null, "path": null, "numbers": [42]}'
    agent = make_agent(prereq_raw=prereq_raw)
    result = await agent.execute([{"role": "User", "content": "Issue 42"}])
    assert "must provide both" in result.lower()
    agent.agents.crew.kickoff_async.assert_not_called()


@pytest.mark.anyio
async def test_gate_repo_without_owner():
    prereq_raw = '{"owner": null, "repo": "myrepo", "ref": null, "path": null, "numbers": null}'
    agent = make_agent(prereq_raw=prereq_raw)
    result = await agent.execute([{"role": "User", "content": "list issues in myrepo"}])
    assert "must also provide an owner" in result.lower()
    agent.agents.crew.kickoff_async.assert_not_called()


@pytest.mark.anyio
async def test_happy_path_researcher_called():
    prereq_raw = '{"owner": "kagenti", "repo": "myrepo", "ref": null, "path": null, "numbers": null}'
    agent = make_agent(prereq_raw=prereq_raw, researcher_raw="done")
    result = await agent.execute([{"role": "User", "content": "list issues in kagenti/myrepo"}])
    agent.agents.crew.kickoff_async.assert_called_once()
    assert result == "done"


@pytest.mark.anyio
async def test_happy_path_researcher_inputs():
    prereq_raw = '{"owner": "kagenti", "repo": "myrepo", "ref": null, "path": null, "numbers": null}'
    agent = make_agent(prereq_raw=prereq_raw)
    await agent.execute([{"role": "User", "content": "list issues in kagenti/myrepo"}])
    call_kwargs = agent.agents.crew.kickoff_async.call_args
    inputs = call_kwargs.kwargs.get("inputs") or call_kwargs.args[0] if call_kwargs.args else {}
    if not inputs:
        inputs = call_kwargs[1].get("inputs", {})
    assert inputs.get("owner") == "kagenti"
    assert inputs.get("repo") == "myrepo"
