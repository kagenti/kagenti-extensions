"""
Tests for the single_role_agent (SingleRoleMapper).

The agent maps a realm role to the set of privileges/scopes it should hold.

To run all tests:
    pytest test/policy/test_single_role_agent.py

To skip integration tests (require LLM access):
    pytest test/policy/test_single_role_agent.py -m "not integration"

To run ONLY integration tests:
    pytest test/policy/test_single_role_agent.py -m integration
"""

import os
from typing import Any
import pytest
import yaml
from pathlib import Path
from unittest.mock import Mock

from aiac.pdp.policy.models import PolicyObjectModel
from aiac.idp.configuration.models import Role, Scope
from single_role_agent import SingleRoleMapper, SingleRoleState
from base_mapper import (
    BaseMappingState,
    extract_explanation_and_json,
    validate_mapping_items,
    should_route_after_structural_validation,
    should_retry_after_semantic,
)
from config.constants import MAX_VALIDATION_RETRIES
from langgraph.graph import END
from config import create_llm


pytestmark = pytest.mark.integration


# ============================================================================
# LOCAL ADAPTERS
# (the underlying base_mapper functions take explicit parameters;
#  these thin wrappers match the state-based calling convention the tests use)
# ============================================================================

def extract_explanation_and_json_single_role_scopes(content: str):
    """Delegate to the shared base_mapper parser."""
    return extract_explanation_and_json(content)


def _validate_role_scopes(
    state: BaseMappingState,
    verbose: bool = True,
    max_retries: int = MAX_VALIDATION_RETRIES,
) -> dict[str, Any]:
    return validate_mapping_items(
        state,
        verbose,
        max_retries,
        items_key="granted_privileges",
        reference_key="privileges",
        item_type_label="privilege",
    )


def _should_route_after_structural_validation(
    state: dict,
    max_retries: int = MAX_VALIDATION_RETRIES,
) -> str:
    return should_route_after_structural_validation(
        validation_passed=state.get("validation_passed", False),
        retry_count=state.get("retry_count", 0),
        max_retries=max_retries,
        analyze_node="analyze_role_scopes",
        verify_node="verify_semantic_scope_mapping",
    )


def _should_retry_after_semantic(
    state: dict,
    max_retries: int = MAX_VALIDATION_RETRIES,
) -> str:
    return should_retry_after_semantic(
        validation_passed=state.get("validation_passed", False),
        retry_count=state.get("retry_count", 0),
        max_retries=max_retries,
        analyze_node="analyze_role_scopes",
    )


# ============================================================================
# FIXTURES
# ============================================================================

@pytest.fixture
def fixtures_dir():
    return Path(__file__).parent.parent / "fixtures"


@pytest.fixture
def config_file():
    return Path(__file__).parent.parent / "fixtures" / "config.yaml"


@pytest.fixture
def policy_files(fixtures_dir):
    return sorted((fixtures_dir / "policies").glob("*.txt"))


@pytest.fixture(params=["claude-haiku", "gpt-nano", "gemini", "gpt-5-mini"])
def llm_model_name(request):
    return request.param


@pytest.fixture
def llm_instance(llm_model_name):
    import socket
    from urllib.parse import urlparse
    from config.llm_config import load_llm_config_from_yaml
    cfg = load_llm_config_from_yaml(llm_model_name)
    if cfg.endpoint:
        parsed = urlparse(cfg.endpoint)
        host = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            with socket.create_connection((host, port), timeout=3.0):
                pass
        except (socket.timeout, OSError) as exc:
            pytest.skip(f"Model {llm_model_name} endpoint not reachable: {exc}")
    return create_llm(model_name=llm_model_name, verbose=False)


@pytest.fixture
def mock_llm():
    return Mock()


@pytest.fixture
def sample_scopes() -> list[Scope]:
    return [
        Scope(id="github-tool-aud", name="github-tool-aud",
              description="Provides access to public GitHub repos"),
        Scope(id="github-full-access", name="github-full-access",
              description="Provides access to private GitHub repos"),
        Scope(id="demo-ui", name="demo-ui",
              description="Access to the demo UI interface"),
    ]


@pytest.fixture
def developer_role() -> Role:
    return Role(id="developer", name="developer",
                description="R&D team members", composite=False)


# ============================================================================
# HELPERS
# ============================================================================

def _make_analysis_response(role_name: str, granted_privileges: list) -> str:
    privs_json = ", ".join(f'"{p}"' for p in granted_privileges)
    return f"""
```explanation
The {role_name} role is a user-facing role. Granting appropriate privileges.
```
```json
{{
  "role": "{role_name}",
  "granted_privileges": [{privs_json}]
}}
```
"""


def _make_verify_response(correct: bool = True) -> str:
    status = "YES" if correct else "NO"
    return f"MAPPING_CORRECT: {status}\nEXPLANATION: Mapping is correct."


def _make_mock_llm(role_name: str, granted_privileges: list) -> Mock:
    mock = Mock()
    analysis = Mock()
    analysis.content = _make_analysis_response(role_name, granted_privileges)
    verify = Mock()
    verify.content = _make_verify_response()
    mock.invoke.side_effect = [analysis, verify]
    return mock


def _make_validate_state(
    granted: list[Scope],
    privileges: list[Scope],
    retry: int = 0,
) -> SingleRoleState:
    return SingleRoleState(
        policy_description="test",
        role=Role(id="developer", name="developer",
                  description="R&D team members", composite=False),
        privileges=privileges,
        explanation="",
        granted_privileges=granted,
        messages=[],
        errors=[],
        retry_count=retry,
        validation_passed=True,
    )


def _make_routing_state(validation_passed: bool, retry_count: int) -> dict:
    return {"validation_passed": validation_passed, "retry_count": retry_count}


# ============================================================================
# UNIT TESTS: extract_explanation_and_json_single_role_scopes
# ============================================================================

_SAMPLE_SCOPES = [
    Scope(id="github-tool-aud", name="github-tool-aud"),
    Scope(id="github-full-access", name="github-full-access"),
    Scope(id="demo-ui", name="demo-ui"),
]


def test_extract_fenced_explanation_and_json():
    """Parser extracts explanation and JSON from properly fenced blocks."""
    content = _make_analysis_response("developer", ["github-tool-aud", "github-full-access"])
    explanation, data = extract_explanation_and_json_single_role_scopes(content)
    assert explanation
    assert data is not None
    assert data.get("granted_privileges") == ["github-tool-aud", "github-full-access"]


def test_extract_json_only_block():
    """Parser extracts JSON from a bare ```json block with no explanation."""
    content = '```json\n{"role": "tech-support", "granted_privileges": ["demo-ui"]}\n```'
    explanation, data = extract_explanation_and_json_single_role_scopes(content)
    assert data is not None
    assert data["granted_privileges"] == ["demo-ui"]


def test_extract_bare_json_object():
    """Parser finds a bare {...} JSON object in the response."""
    content = 'Here is the result: {"role": "sales", "granted_privileges": []}'
    explanation, data = extract_explanation_and_json_single_role_scopes(content)
    assert data is not None
    assert data["granted_privileges"] == []
    assert "Here is the result:" in explanation


def test_extract_returns_none_on_invalid_json():
    """Parser returns (empty_str, None) when no valid JSON is found."""
    content = "This response has no JSON at all."
    explanation, data = extract_explanation_and_json_single_role_scopes(content)
    assert data is None


def test_extract_empty_granted_privileges():
    """Parser handles empty granted_privileges list."""
    content = '```json\n{"role": "sales", "granted_privileges": []}\n```'
    explanation, data = extract_explanation_and_json_single_role_scopes(content)
    assert data is not None
    assert data["granted_privileges"] == []


# ============================================================================
# UNIT TESTS: _validate_role_scopes
# ============================================================================

def test_validate_passes_with_valid_privileges():
    """Validation succeeds when all granted privileges are in the available list."""
    state = _make_validate_state(
        [Scope(id="github-tool-aud", name="github-tool-aud")],
        _SAMPLE_SCOPES,
    )
    result = _validate_role_scopes(state, verbose=False, max_retries=3)
    assert result["validation_passed"] is True
    assert result["errors"] == []


def test_validate_rejects_unknown_privilege():
    """Validation fails when an unknown privilege name is returned."""
    state = _make_validate_state(
        [Scope(id="nonexistent-priv", name="nonexistent-priv")],
        _SAMPLE_SCOPES,
    )
    result = _validate_role_scopes(state, verbose=False, max_retries=3)
    assert result["validation_passed"] is False
    assert any("nonexistent-priv" in e for e in result["errors"])


def test_validate_rejects_duplicates():
    """Validation fails when duplicate privilege names appear in the result."""
    dup = Scope(id="github-tool-aud", name="github-tool-aud")
    state = _make_validate_state([dup, dup], _SAMPLE_SCOPES)
    result = _validate_role_scopes(state, verbose=False, max_retries=3)
    assert result["validation_passed"] is False
    assert any("Duplicate" in e for e in result["errors"])


def test_validate_passes_with_empty_granted():
    """Validation passes when no privileges are granted (empty list is valid)."""
    state = _make_validate_state([], _SAMPLE_SCOPES)
    result = _validate_role_scopes(state, verbose=False, max_retries=3)
    assert result["validation_passed"] is True
    assert result["errors"] == []


def test_validate_increments_retry_count_on_failure():
    """Retry count is incremented when validation fails and retries remain."""
    state = _make_validate_state(
        [Scope(id="bad-priv", name="bad-priv")], _SAMPLE_SCOPES, retry=0
    )
    result = _validate_role_scopes(state, verbose=False, max_retries=3)
    assert result["retry_count"] == 1
    assert result["validation_passed"] is False


def test_validate_does_not_increment_retry_when_exhausted():
    """Retry count is not further incremented once max_retries is reached."""
    state = _make_validate_state(
        [Scope(id="bad-priv", name="bad-priv")], _SAMPLE_SCOPES, retry=3
    )
    result = _validate_role_scopes(state, verbose=False, max_retries=3)
    assert result["retry_count"] == 3
    assert result["validation_passed"] is False


# ============================================================================
# UNIT TESTS: routing functions
# ============================================================================

def test_route_after_structural_proceeds_to_verify_on_success():
    """Routes to verify_semantic_scope_mapping when structural validation passed."""
    route = _should_route_after_structural_validation(
        _make_routing_state(validation_passed=True, retry_count=0), max_retries=3
    )
    assert route == "verify_semantic_scope_mapping"


def test_route_after_structural_retries_when_failed_and_retries_remain():
    """Routes back to analyze_role_scopes on failure when retries are available."""
    route = _should_route_after_structural_validation(
        _make_routing_state(validation_passed=False, retry_count=1), max_retries=3
    )
    assert route == "analyze_role_scopes"


def test_route_after_structural_ends_when_retries_exhausted():
    """Routes to END when structural validation failed and retries are exhausted."""
    route = _should_route_after_structural_validation(
        _make_routing_state(validation_passed=False, retry_count=3), max_retries=3
    )
    assert route == END


def test_route_after_semantic_retries_when_failed():
    """Routes back to analyze_role_scopes when semantic check fails and retries remain."""
    route = _should_retry_after_semantic(
        _make_routing_state(validation_passed=False, retry_count=0), max_retries=3
    )
    assert route == "analyze_role_scopes"


def test_route_after_semantic_ends_when_passed():
    """Routes to END when semantic verification passed."""
    route = _should_retry_after_semantic(
        _make_routing_state(validation_passed=True, retry_count=0), max_retries=3
    )
    assert route == END


def test_route_after_semantic_ends_when_retries_exhausted():
    """Routes to END when semantic check failed but retries are exhausted."""
    route = _should_retry_after_semantic(
        _make_routing_state(validation_passed=False, retry_count=3), max_retries=3
    )
    assert route == END


# ============================================================================
# UNIT TESTS: SingleRoleMapper
# ============================================================================

def test_single_role_mapper_get_graph(developer_role, sample_scopes, mock_llm):
    """get_graph() returns the compiled LangGraph workflow."""
    mapper = SingleRoleMapper(role=developer_role, privileges=sample_scopes, llm=mock_llm, verbose=False)
    assert mapper.get_graph() is not None


def test_map_privileges_returns_correct_keys(developer_role, sample_scopes):
    """map_privileges() returns a dict with all expected keys."""
    mock = _make_mock_llm("developer", ["github-tool-aud"])
    mapper = SingleRoleMapper(role=developer_role, privileges=sample_scopes, llm=mock, verbose=False)
    result = mapper.map_privileges(policy_description="Developers get GitHub access.")
    for key in ("policy_description", "role", "granted_privileges", "explanation",
                "errors", "success", "retry_count"):
        assert key in result, f"Missing key: {key}"
    assert result["role"].name == "developer"


def test_map_privileges_success_flag_on_clean_run(developer_role, sample_scopes):
    """map_privileges() sets success=True and errors=[] on a clean run."""
    mock = _make_mock_llm("developer", ["github-tool-aud"])
    mapper = SingleRoleMapper(role=developer_role, privileges=sample_scopes, llm=mock, verbose=False)
    result = mapper.map_privileges(policy_description="Developers get GitHub access.")
    assert result["success"] is True
    assert result["errors"] == []


def test_map_privileges_returns_granted_privileges(developer_role, sample_scopes):
    """map_privileges() returns the granted Scope objects from the LLM response."""
    mock = _make_mock_llm("developer", ["github-tool-aud", "github-full-access"])
    mapper = SingleRoleMapper(role=developer_role, privileges=sample_scopes, llm=mock, verbose=False)
    result = mapper.map_privileges(policy_description="Developers get full GitHub access.")
    names = {s.name for s in result["granted_privileges"]}
    assert "github-tool-aud" in names
    assert "github-full-access" in names


def test_map_privileges_with_empty_granted_privileges(sample_scopes):
    """map_privileges() handles empty granted_privileges (non-user-facing role)."""
    role = Role(id="sales", name="sales", description="Sales team members", composite=False)
    mock = _make_mock_llm("sales", [])
    mapper = SingleRoleMapper(role=role, privileges=sample_scopes, llm=mock, verbose=False)
    result = mapper.map_privileges(policy_description="Sales staff have no GitHub access.")
    assert result["granted_privileges"] == []
    assert result["success"] is True


def test_generate_policy_returns_policy_model(developer_role, sample_scopes):
    """generate_policy() returns a PolicyObjectModel with rules and explanation."""
    mock = _make_mock_llm("developer", ["github-tool-aud"])
    mapper = SingleRoleMapper(role=developer_role, privileges=sample_scopes, llm=mock, verbose=False)
    result = mapper.generate_policy("Developers get GitHub access.")
    assert isinstance(result, PolicyObjectModel)
    assert isinstance(result.rules, list)


def test_generate_policy_yaml_is_valid_yaml(developer_role, sample_scopes):
    """generate_policy() produces a valid PolicyObjectModel with expected rules."""
    mock = _make_mock_llm("developer", ["github-tool-aud"])
    mapper = SingleRoleMapper(role=developer_role, privileges=sample_scopes, llm=mock, verbose=False)
    policy = mapper.generate_policy("Developers get GitHub access.")
    assert isinstance(policy, PolicyObjectModel)
    assert len(policy.rules) > 0
    assert any(r.role.name == "developer" for r in policy.rules)


def test_generate_policy_maps_privilege_to_role(developer_role, sample_scopes):
    """generate_policy() produces a Rule mapping the role to the granted privilege."""
    mock = _make_mock_llm("developer", ["github-tool-aud"])
    mapper = SingleRoleMapper(role=developer_role, privileges=sample_scopes, llm=mock, verbose=False)
    rules, explanation = mapper.generate_policy("Developers get GitHub access.")
    assert any(
        r.role.name == "developer" and r.scope.name == "github-tool-aud"
        for r in rules
    )


def test_generate_policy_with_unknown_privilege_raises_value_error(developer_role, sample_scopes):
    """generate_policy() raises ValueError when the LLM returns an unknown privilege."""
    bad_response = Mock()
    bad_response.content = _make_analysis_response("developer", ["nonexistent-priv"])
    mock = Mock()
    mock.invoke.return_value = bad_response
    mapper = SingleRoleMapper(role=developer_role, privileges=sample_scopes, llm=mock, verbose=False)
    with pytest.raises(ValueError):
        mapper.generate_policy("Some description.")


# ============================================================================
# FIXTURE SANITY CHECK
# ============================================================================

def test_fixture_files_exist(fixtures_dir):
    policies_dir = fixtures_dir / "policies"
    expected_dir = fixtures_dir / "expected"
    assert policies_dir.exists()
    assert expected_dir.exists()
    policy_files = list(policies_dir.glob("*.txt"))
    assert len(policy_files) > 0
    for policy_file in policy_files:
        expected_file = expected_dir / f"{policy_file.stem}.yaml"
        assert expected_file.exists()
        try:
            yaml.safe_load(expected_file.read_text())
        except yaml.YAMLError as exc:
            pytest.fail(f"Invalid YAML in {expected_file}: {exc}")


# ============================================================================
# INTEGRATION TEST (requires LLM)
# ============================================================================

def test_generate_single_role_from_fixtures(
    fixtures_dir, config_file, policy_files, llm_instance, llm_model_name
):
    """Integration: map each realm role for each policy fixture using a real LLM."""
    if not policy_files:
        pytest.skip("No policy fixture files found")

    os.environ["AIAC_PDP_CONFIG_PATH"] = str(config_file)
    from aiac.pdp.library.read_api_from_config import Configuration
    config_api = Configuration.for_realm("demo")
    roles = config_api.get_roles()
    scopes = config_api.get_scopes()

    failures = []

    for policy_file in policy_files:
        policy_description = policy_file.read_text().strip()
        expected_file = fixtures_dir / "expected" / f"{policy_file.stem}.yaml"

        if not expected_file.exists():
            failures.append(
                f"[{llm_model_name}] {policy_file.name}: missing expected file {expected_file}"
            )
            continue

        expected_full = yaml.safe_load(expected_file.read_text()).get("policy", {})

        for role in roles:
            expected_for_role = expected_full.get(role.name, [])
            expected_privileges = {m["privilege"] for m in expected_for_role}

            try:
                mapper = SingleRoleMapper(
                    role=role,
                    privileges=scopes,
                    llm=llm_instance,
                    verbose=False,
                )
                result = mapper.map_privileges(policy_description=policy_description)
                generated = {s.name for s in result.get("granted_privileges", [])}
                missing = expected_privileges - generated
                extra = generated - expected_privileges

                if missing or extra:
                    diffs = (
                        [f"  Missing privilege: '{p}'" for p in sorted(missing)]
                        + [f"  Extra privilege:   '{p}'" for p in sorted(extra)]
                    )
                    failures.append(
                        f"[{llm_model_name}] {policy_file.name} / role={role.name}:\n"
                        + "\n".join(diffs)
                    )

            except Exception as exc:
                failures.append(
                    f"[{llm_model_name}] {policy_file.name} / role={role.name}: exception: {exc}"
                )

    if failures:
        pytest.fail(
            f"Single role mapper tests failed for model {llm_model_name}:\n\n"
            + "\n\n".join(failures)
        )
