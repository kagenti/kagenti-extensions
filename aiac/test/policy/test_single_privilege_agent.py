"""
Tests for the single_privilege_agent (SinglePrivilegeMapper).

The agent maps a single privilege/scope to the set of realm roles that
should have access to it.

To run all tests:
    pytest test/policy/test_single_privilege_agent.py

To skip integration tests (require LLM access):
    pytest test/policy/test_single_privilege_agent.py -m "not integration"

To run ONLY integration tests:
    pytest test/policy/test_single_privilege_agent.py -m integration
"""

import os
from typing import Any
import pytest
import yaml
from pathlib import Path
from unittest.mock import Mock

from aiac.pdp.policy.models import PolicyObjectModel
from aiac.pdp.library.configuration.models import Role, Scope
from base_mapper.state import BaseMappingState
from single_privilege_agent import SinglePrivilegeMapper, SinglePrivilegeState
from base_mapper import (
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
# (base_mapper functions take explicit params; these wrappers use state dicts)
# ============================================================================

def extract_explanation_and_json_single_privilege_roles(content: str):
    """Delegate to the shared base_mapper parser."""
    return extract_explanation_and_json(content)


def _validate_privilege_roles(
    state: SinglePrivilegeState,
    verbose: bool = True,
    max_retries: int = MAX_VALIDATION_RETRIES,
) -> dict[str, Any]:
    return validate_mapping_items(
        state,
        verbose,
        max_retries,
        items_key="roles_with_access",
        reference_key="roles",
        item_type_label="role",
    )


def _should_route_after_structural_validation(
    state: dict,
    max_retries: int = MAX_VALIDATION_RETRIES,
) -> str:
    return should_route_after_structural_validation(
        validation_passed=state.get("validation_passed", False),
        retry_count=state.get("retry_count", 0),
        max_retries=max_retries,
        analyze_node="analyze_role_mapping",
        verify_node="verify_semantic_mapping",
    )


def _should_retry_after_semantic(
    state: dict,
    max_retries: int = MAX_VALIDATION_RETRIES,
) -> str:
    return should_retry_after_semantic(
        validation_passed=state.get("validation_passed", False),
        retry_count=state.get("retry_count", 0),
        max_retries=max_retries,
        analyze_node="analyze_role_mapping",
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


@pytest.fixture(params=["claude-haiku", "gpt-nano", "gemini", "gpt-oss"])
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
def sample_roles() -> list[Role]:
    return [
        Role(id="developer", name="developer",
             description="R&D team members", composite=False),
        Role(id="tech-support", name="tech-support",
             description="Technical support staff", composite=False),
        Role(id="sales", name="sales",
             description="Sales team members", composite=False),
    ]


@pytest.fixture
def github_aud_privilege() -> Scope:
    return Scope(
        id="github-tool-aud",
        name="github-tool-aud",
        description="Provides access to public GitHub repositories",
    )


# ============================================================================
# HELPERS
# ============================================================================

def _make_analysis_response(privilege_name: str, roles_with_access: list) -> str:
    roles_json = ", ".join(f'"{r}"' for r in roles_with_access)
    return f"""
```explanation
The privilege '{privilege_name}' should be granted to those with a technical need.
```
```json
{{
  "privilege": "{privilege_name}",
  "roles_with_access": [{roles_json}]
}}
```
"""


def _make_verify_response(correct: bool = True) -> str:
    status = "YES" if correct else "NO"
    return f"MAPPING_CORRECT: {status}\nEXPLANATION: Mapping is correct."


def _make_mock_llm(privilege_name: str, roles_with_access: list) -> Mock:
    mock = Mock()
    analysis = Mock()
    analysis.content = _make_analysis_response(privilege_name, roles_with_access)
    verify = Mock()
    verify.content = _make_verify_response()
    mock.invoke.side_effect = [analysis, verify]
    return mock


def _make_validate_state(
    roles_with_access: list[Role],
    all_roles: list[Role],
    retry: int = 0,
) -> SinglePrivilegeState:
    return {
        "policy_description": "test",
        "privilege": Scope(id="github-tool-aud", name="github-tool-aud"),
        "roles": all_roles,
        "explanation": "",
        "roles_with_access": roles_with_access,
        "messages": [],
        "errors": [],
        "retry_count": retry,
        "validation_passed": True,
    }


def _make_routing_state(validation_passed: bool, retry_count: int) -> dict:
    return {"validation_passed": validation_passed, "retry_count": retry_count}


_SAMPLE_ROLES = [
    Role(id="developer", name="developer", description="R&D", composite=False),
    Role(id="tech-support", name="tech-support", description="Support", composite=False),
    Role(id="sales", name="sales", description="Sales", composite=False),
]


# ============================================================================
# UNIT TESTS: extract_explanation_and_json_single_privilege_roles
# ============================================================================

def test_extract_fenced_explanation_and_json():
    """Parser extracts explanation and JSON from properly fenced blocks."""
    content = _make_analysis_response("github-tool-aud", ["developer", "tech-support"])
    explanation, data = extract_explanation_and_json_single_privilege_roles(content)
    assert explanation
    assert data is not None
    assert data.get("roles_with_access") == ["developer", "tech-support"]


def test_extract_json_only_block():
    """Parser extracts JSON from a bare ```json block with no explanation."""
    content = '```json\n{"privilege": "demo-ui", "roles_with_access": ["sales"]}\n```'
    explanation, data = extract_explanation_and_json_single_privilege_roles(content)
    assert data is not None
    assert data["roles_with_access"] == ["sales"]


def test_extract_bare_json_object():
    """Parser finds a bare {...} JSON object in the response."""
    content = 'Result: {"privilege": "demo-ui", "roles_with_access": []}'
    explanation, data = extract_explanation_and_json_single_privilege_roles(content)
    assert data is not None
    assert data["roles_with_access"] == []


def test_extract_returns_none_on_invalid_json():
    """Parser returns (empty_str, None) when no valid JSON is found."""
    content = "This response has no JSON at all."
    explanation, data = extract_explanation_and_json_single_privilege_roles(content)
    assert data is None


def test_extract_empty_roles_with_access():
    """Parser handles empty roles_with_access list."""
    content = '```json\n{"privilege": "demo-ui", "roles_with_access": []}\n```'
    explanation, data = extract_explanation_and_json_single_privilege_roles(content)
    assert data is not None
    assert data["roles_with_access"] == []


# ============================================================================
# UNIT TESTS: _validate_privilege_roles
# ============================================================================

def test_validate_passes_with_valid_roles():
    """Validation succeeds when all roles_with_access are in the reference list."""
    state = _make_validate_state(
        [Role(id="developer", name="developer", composite=False)],
        _SAMPLE_ROLES,
    )
    result = _validate_privilege_roles(state, verbose=False, max_retries=3)
    assert result["validation_passed"] is True
    assert result["errors"] == []


def test_validate_rejects_unknown_role():
    """Validation fails when an unknown role name is returned."""
    state = _make_validate_state(
        [Role(id="admin", name="admin", composite=False)],
        _SAMPLE_ROLES,
    )
    result = _validate_privilege_roles(state, verbose=False, max_retries=3)
    assert result["validation_passed"] is False
    assert any("admin" in e for e in result["errors"])


def test_validate_rejects_duplicates():
    """Validation fails when duplicate role names appear in the result."""
    dup = Role(id="developer", name="developer", composite=False)
    state = _make_validate_state([dup, dup], _SAMPLE_ROLES)
    result = _validate_privilege_roles(state, verbose=False, max_retries=3)
    assert result["validation_passed"] is False
    assert any("Duplicate" in e for e in result["errors"])


def test_validate_passes_with_empty_roles_with_access():
    """Validation passes when no roles are granted (empty list is valid)."""
    state = _make_validate_state([], _SAMPLE_ROLES)
    result = _validate_privilege_roles(state, verbose=False, max_retries=3)
    assert result["validation_passed"] is True
    assert result["errors"] == []


def test_validate_increments_retry_count_on_failure():
    """Retry count is incremented when validation fails and retries remain."""
    state = _make_validate_state(
        [Role(id="unknown", name="unknown", composite=False)], _SAMPLE_ROLES, retry=0
    )
    result = _validate_privilege_roles(state, verbose=False, max_retries=3)
    assert result["retry_count"] == 1
    assert result["validation_passed"] is False


def test_validate_does_not_increment_retry_when_exhausted():
    """Retry count is not further incremented once max_retries is reached."""
    state = _make_validate_state(
        [Role(id="unknown", name="unknown", composite=False)], _SAMPLE_ROLES, retry=3
    )
    result = _validate_privilege_roles(state, verbose=False, max_retries=3)
    assert result["retry_count"] == 3
    assert result["validation_passed"] is False


# ============================================================================
# UNIT TESTS: routing functions
# ============================================================================

def test_route_after_structural_proceeds_to_verify_on_success():
    """Routes to verify_semantic_mapping when structural validation passed."""
    route = _should_route_after_structural_validation(
        _make_routing_state(validation_passed=True, retry_count=0), max_retries=3
    )
    assert route == "verify_semantic_mapping"


def test_route_after_structural_retries_when_failed_and_retries_remain():
    """Routes back to analyze_role_mapping on failure when retries are available."""
    route = _should_route_after_structural_validation(
        _make_routing_state(validation_passed=False, retry_count=1), max_retries=3
    )
    assert route == "analyze_role_mapping"


def test_route_after_structural_ends_when_retries_exhausted():
    """Routes to END when structural validation failed and retries are exhausted."""
    route = _should_route_after_structural_validation(
        _make_routing_state(validation_passed=False, retry_count=3), max_retries=3
    )
    assert route == END


def test_route_after_semantic_retries_when_failed():
    """Routes back to analyze_role_mapping when semantic check fails and retries remain."""
    route = _should_retry_after_semantic(
        _make_routing_state(validation_passed=False, retry_count=0), max_retries=3
    )
    assert route == "analyze_role_mapping"


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
# UNIT TESTS: SinglePrivilegeMapper
# ============================================================================

def test_single_privilege_mapper_get_graph(github_aud_privilege, sample_roles, mock_llm):
    """get_graph() returns the compiled LangGraph workflow."""
    mapper = SinglePrivilegeMapper(
        privilege=github_aud_privilege, roles=sample_roles, llm=mock_llm, verbose=False
    )
    assert mapper.get_graph() is not None


def test_map_roles_returns_correct_keys(github_aud_privilege, sample_roles):
    """map_roles() returns a dict with all expected keys."""
    mock = _make_mock_llm("github-tool-aud", ["developer"])
    mapper = SinglePrivilegeMapper(
        privilege=github_aud_privilege, roles=sample_roles, llm=mock, verbose=False
    )
    result = mapper.map_roles(policy_description="Developers get GitHub access.")
    for key in ("policy_description", "privilege", "roles_with_access", "explanation",
                "errors", "success", "retry_count"):
        assert key in result, f"Missing key: {key}"
    assert result["privilege"].name == "github-tool-aud"


def test_map_roles_success_flag_on_clean_run(github_aud_privilege, sample_roles):
    """map_roles() sets success=True and errors=[] on a clean run."""
    mock = _make_mock_llm("github-tool-aud", ["developer"])
    mapper = SinglePrivilegeMapper(
        privilege=github_aud_privilege, roles=sample_roles, llm=mock, verbose=False
    )
    result = mapper.map_roles(policy_description="Developers get GitHub access.")
    assert result["success"] is True
    assert result["errors"] == []


def test_map_roles_returns_roles_with_access(github_aud_privilege, sample_roles):
    """map_roles() returns the matching Role objects from the LLM response."""
    mock = _make_mock_llm("github-tool-aud", ["developer", "tech-support"])
    mapper = SinglePrivilegeMapper(
        privilege=github_aud_privilege, roles=sample_roles, llm=mock, verbose=False
    )
    result = mapper.map_roles(policy_description="Developers and tech-support get GitHub access.")
    names = {r.name for r in result["roles_with_access"]}
    assert "developer" in names
    assert "tech-support" in names


def test_map_roles_with_empty_access(github_aud_privilege, sample_roles):
    """map_roles() handles empty roles_with_access (privilege is internal-only)."""
    mock = _make_mock_llm("github-tool-aud", [])
    mapper = SinglePrivilegeMapper(
        privilege=github_aud_privilege, roles=sample_roles, llm=mock, verbose=False
    )
    result = mapper.map_roles(policy_description="This privilege is internal only.")
    assert result["roles_with_access"] == []
    assert result["success"] is True


def test_generate_policy_returns_policy_model(github_aud_privilege, sample_roles):
    """generate_policy() returns a PolicyObjectModel with rules and explanation."""
    mock = _make_mock_llm("github-tool-aud", ["developer"])
    mapper = SinglePrivilegeMapper(
        privilege=github_aud_privilege, roles=sample_roles, llm=mock, verbose=False
    )
    result = mapper.generate_policy("Developers get GitHub access.")
    assert isinstance(result, PolicyObjectModel)
    assert isinstance(result.rules, list)


def test_generate_policy_maps_role_to_privilege(github_aud_privilege, sample_roles):
    """generate_policy() produces Rules mapping the granted roles to the privilege."""
    mock = _make_mock_llm("github-tool-aud", ["developer"])
    mapper = SinglePrivilegeMapper(
        privilege=github_aud_privilege, roles=sample_roles, llm=mock, verbose=False
    )
    result = mapper.generate_policy("Developers get GitHub access.")
    assert any(
        r.role.name == "developer" and r.scope.name == "github-tool-aud"
        for r in result.rules
    )


def test_generate_policy_yaml_is_valid_yaml(github_aud_privilege, sample_roles):
    """generate_policy() produces a valid PolicyObjectModel with expected rules."""
    mock = _make_mock_llm("github-tool-aud", ["developer"])
    mapper = SinglePrivilegeMapper(
        privilege=github_aud_privilege, roles=sample_roles, llm=mock, verbose=False
    )
    policy = mapper.generate_policy("Developers get GitHub access.")
    assert isinstance(policy, PolicyObjectModel)
    assert len(policy.rules) > 0
    assert any(r.scope.name == "github-tool-aud" for r in policy.rules)


def test_generate_policy_with_unknown_role_raises_value_error(github_aud_privilege, sample_roles):
    """generate_policy() raises ValueError when the LLM returns an unknown role."""
    bad_response = Mock()
    bad_response.content = _make_analysis_response("github-tool-aud", ["nonexistent-role"])
    mock = Mock()
    mock.invoke.return_value = bad_response
    mapper = SinglePrivilegeMapper(
        privilege=github_aud_privilege, roles=sample_roles, llm=mock, verbose=False
    )
    with pytest.raises(ValueError):
        mapper.generate_policy("Some description.")


def test_single_privilege_mapper_multiple_privileges(sample_roles):
    """Multiple SinglePrivilegeMapper instances can run independently per privilege."""
    demo_ui = Scope(id="demo-ui", name="demo-ui", description="Access to demo UI")
    github_full = Scope(id="github-full-access", name="github-full-access",
                        description="Full GitHub access")

    mock_ui = _make_mock_llm("demo-ui", ["sales", "tech-support"])
    mock_gh = _make_mock_llm("github-full-access", ["developer"])

    ui_mapper = SinglePrivilegeMapper(privilege=demo_ui, roles=sample_roles, llm=mock_ui, verbose=False)
    gh_mapper = SinglePrivilegeMapper(privilege=github_full, roles=sample_roles, llm=mock_gh, verbose=False)

    ui_result = ui_mapper.map_roles("UI is for sales and support.")
    gh_result = gh_mapper.map_roles("GitHub full access is only for developers.")

    assert {r.name for r in ui_result["roles_with_access"]} == {"sales", "tech-support"}
    assert {r.name for r in gh_result["roles_with_access"]} == {"developer"}


# ============================================================================
# INTEGRATION TEST (requires LLM)
# ============================================================================

def test_generate_single_privilege_from_fixtures(
    fixtures_dir, config_file, policy_files, llm_instance, llm_model_name
):
    """Integration: map each privilege to realm roles for each policy fixture using a real LLM."""
    if not policy_files:
        pytest.skip("No policy fixture files found")

    os.environ["AIAC_PDP_CONFIG_PATH"] = str(config_file)
    from aiac.pdp.library.read_api_from_config import Configuration
    config_api = Configuration.for_realm("demo")
    roles = config_api.get_roles()
    services = config_api.get_services()

    all_privileges = [
        (scope, service.name or service.id)
        for service in services
        for scope in service.scopes
        if scope.description
    ]

    failures = []

    for policy_file in policy_files:
        policy_description = policy_file.read_text().strip()
        expected_file = fixtures_dir / "expected" / f"{policy_file.stem}.yaml"

        if not expected_file.exists():
            failures.append(
                f"[{llm_model_name}] {policy_file.name}: missing expected file {expected_file}"
            )
            continue

        expected_policy = yaml.safe_load(expected_file.read_text()).get("policy", {})

        for privilege, service_name in all_privileges:
            expected_roles_for_priv = set()
            for role_name, mappings in expected_policy.items():
                for mapping in mappings:
                    if mapping.get("privilege") == privilege.name:
                        expected_roles_for_priv.add(role_name)

            try:
                mapper = SinglePrivilegeMapper(
                    privilege=privilege,
                    roles=roles,
                    llm=llm_instance,
                    verbose=False,
                )
                result = mapper.map_roles(policy_description=policy_description)
                generated = {r.name for r in result.get("roles_with_access", [])}
                missing = expected_roles_for_priv - generated
                extra = generated - expected_roles_for_priv

                if missing or extra:
                    diffs = (
                        [f"  Missing role: '{r}'" for r in sorted(missing)]
                        + [f"  Extra role:   '{r}'" for r in sorted(extra)]
                    )
                    failures.append(
                        f"[{llm_model_name}] {policy_file.name} / privilege={privilege.name}:\n"
                        + "\n".join(diffs)
                    )

            except Exception as exc:
                failures.append(
                    f"[{llm_model_name}] {policy_file.name} / privilege={privilege.name}: exception: {exc}"
                )

    if failures:
        pytest.fail(
            f"Single privilege mapper tests failed for model {llm_model_name}:\n\n"
            + "\n\n".join(failures)
        )
