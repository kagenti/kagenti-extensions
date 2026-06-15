"""
Tests for the service_policy_agent.

The agent takes a natural language policy description plus a service ID and
produces a partial policy containing only the rules relevant to that service.

To run all tests:
    pytest test/test_service_policy_agent.py

To skip integration tests (require LLM access):
    pytest test/test_service_policy_agent.py -m "not integration"

To run ONLY integration tests:
    pytest test/test_service_policy_agent.py -m integration

To run the LLM-backed fixture test:
    1. Ensure LLM is configured in config/llm.env
    2. Remove the @pytest.mark.skip decorator on test_generate_service_policy_from_fixtures
    3. Run: pytest test/test_service_policy_agent.py::test_generate_service_policy_from_fixtures -v
"""

import pytest
import yaml
from pathlib import Path
from unittest.mock import Mock

from service_policy_agent import ServicePolicyBuilder
from service_policy_agent.graph import _generate_yaml, _build_policy, _filter_and_extract_scopes
from service_policy_agent.state import ServicePolicyState
from config import create_llm


# Mark all tests in this module as integration tests
pytestmark = pytest.mark.integration


# ============================================================================
# FIXTURES
# ============================================================================

@pytest.fixture
def fixtures_dir():
    """Return path to test fixtures directory."""
    return Path(__file__).parent.parent / "fixtures"


@pytest.fixture
def config_file():
    """Return path to the main config.yaml file."""
    return Path(__file__).parent.parent / "fixtures" / "config.yaml"


@pytest.fixture
def policy_files(fixtures_dir):
    """Return list of policy text files to test."""
    return sorted((fixtures_dir / "policies").glob("*.txt"))


@pytest.fixture(params=[
    "claude-haiku",
    "gpt-nano",
    "gemini",
    "gpt-oss",
])
def llm_model_name(request):
    """Return model name for parametrised testing."""
    return request.param


@pytest.fixture
def llm_instance(llm_model_name):
    """Create LLM instance from YAML config."""
    return create_llm(model_name=llm_model_name, verbose=False)


@pytest.fixture
def mock_llm():
    """Return a bare Mock that can stand in for a LangChain LLM."""
    return Mock()


# ============================================================================
# HELPERS
# ============================================================================

def normalize_policy_yaml(yaml_content: str) -> dict:
    """Parse YAML and extract the 'policy' sub-dict for comparison."""
    data = yaml.safe_load(yaml_content)
    return data.get("policy", {})


def filter_policy_to_service(policy: dict, service_id: str) -> dict:
    """
    Keep only the realm-role entries that contain at least one mapping for
    *service_id*.  Within each kept entry, retain only the mappings for that
    service.  Used to derive the expected partial policy from a full fixture.
    """
    result = {}
    for realm_role, mappings in policy.items():
        service_mappings = [m for m in mappings if m.get("service") == service_id]
        if service_mappings:
            result[realm_role] = service_mappings
    return result


def compare_policies(generated: dict, expected: dict) -> tuple[bool, list[str]]:
    """
    Require exact equality between *generated* and *expected* policy dicts.

    Returns:
        (match: bool, differences: list[str])
    """
    differences = []

    generated_roles = set(generated.keys())
    expected_roles = set(expected.keys())

    for role in expected_roles - generated_roles:
        differences.append(f"Missing realm role: '{role}'")

    for role in generated_roles - expected_roles:
        differences.append(f"Unexpected extra realm role: '{role}'")

    for role in expected_roles & generated_roles:
        gen_set = {(m["service"], m["privilege"]) for m in generated[role]}
        exp_set = {(m["service"], m["privilege"]) for m in expected[role]}

        for mapping in exp_set - gen_set:
            differences.append(f"Role '{role}' missing mapping: {mapping}")

        for mapping in gen_set - exp_set:
            differences.append(f"Role '{role}' has unexpected extra mapping: {mapping}")

    return len(differences) == 0, differences


# ============================================================================
# UNIT TESTS (no LLM required)
# ============================================================================

def test_generate_yaml_unit():
    """_generate_yaml renders YAML with correct header comments and structure (bypasses LLM)."""
    state: ServicePolicyState = {
        "description": "Developers get full GitHub access.",
        "service_id": "github-tool",
        "explanation": "Developer realm role maps to all github-tool roles.",
        "policy_structure": {
            "policy": {
                "developer": [
                    {"service": "github-tool", "role": "github-tool-aud"},
                    {"service": "github-tool", "role": "github-full-access"},
                ]
            }
        },
        "parsed_scopes": [],
        "yaml_output": "",
        "messages": [],
        "errors": [],
        "retry_count": 0,
        "validation_passed": True,
    }

    result = _generate_yaml(state)

    output = result["yaml_output"]
    assert "policy:" in output
    assert "developer:" in output
    assert "github-tool" in output
    assert "github-full-access" in output
    # Header must mention the scoped service
    assert "github-tool" in output
    assert "# Partial Access Control Policy" in output
    assert "# Original Policy Description:" in output
    assert "Developers get full GitHub access." in output


def test_build_policy_unit():
    """_build_policy assembles policy_structure correctly from parsed_scopes (bypasses LLM)."""
    state: ServicePolicyState = {
        "description": "test",
        "service_id": "github-tool",
        "explanation": "",
        "parsed_scopes": [
            {
                "role": "developer",
                "privileges": [
                    {"service": "github-tool", "privilege": "github-full-access"},
                    {"service": "github-tool", "privilege": "github-tool-aud"},
                ],
            },
            {
                "role": "tech-support",
                "privileges": [
                    {"service": "github-tool", "privilege": "github-tool-aud"},
                ],
            },
        ],
        "policy_structure": {},
        "yaml_output": "",
        "messages": [],
        "errors": [],
        "retry_count": 0,
        "validation_passed": True,
    }

    result = _build_policy(state)

    policy = result["policy_structure"]["policy"]
    assert "developer" in policy
    assert "tech-support" in policy
    assert {"service": "github-tool", "privilege": "github-full-access"} in policy["developer"]
    assert {"service": "github-tool", "privilege": "github-tool-aud"} in policy["tech-support"]
    # No other services should appear
    all_services = {m["service"] for mappings in policy.values() for m in mappings}
    assert all_services == {"github-tool"}


def test_build_policy_empty_scopes():
    """_build_policy produces an empty policy when no scopes matched (bypasses LLM)."""
    state: ServicePolicyState = {
        "description": "test",
        "service_id": "kagenti",
        "explanation": "",
        "parsed_scopes": [],
        "policy_structure": {},
        "yaml_output": "",
        "messages": [],
        "errors": [],
        "retry_count": 0,
        "validation_passed": True,
    }

    result = _build_policy(state)
    assert result["policy_structure"] == {"policy": {}}

def test_service_policy_builder_initialization(config_file, mock_llm):
    """ServicePolicyBuilder loads only roles for the specified service."""

    builder = ServicePolicyBuilder(
        service_id="github-tool",
        config_path=config_file,
        llm=mock_llm,
        verbose=False,
    )

    assert builder.service_id == "github-tool"
    # Realm roles come from config regardless of scoping
    realm_role_names = [r["name"] for r in builder.realm_roles]
    assert "developer" in realm_role_names
    assert "tech-support" in realm_role_names

    # Only github-tool privileges should be loaded
    privilege_names = [p["name"] for p in builder.privileges]
    assert "github-tool-aud" in privilege_names
    assert "github-full-access" in privilege_names
    # Privileges from other services must not appear
    assert "demo-ui" not in privilege_names
    assert "github-agent" not in privilege_names

def test_service_policy_builder_initialization_unknown_service(config_file, mock_llm):
    """ServicePolicyBuilder with an unknown service_id yields an empty privilege list."""

    builder = ServicePolicyBuilder(
        service_id="does-not-exist",
        config_path=config_file,
        llm=mock_llm,
        verbose=False,
    )

    assert builder.privileges == []


def test_get_graph_returns_compiled_graph(config_file, mock_llm):
    """get_graph() returns the compiled LangGraph workflow."""
    builder = ServicePolicyBuilder(
        service_id="github-tool",
        config_path=config_file,
        llm=mock_llm,
        verbose=False,
    )
    graph = builder.get_graph()
    assert graph is not None




# ============================================================================
# MOCK-LLM TESTS (structural / format validation, no real LLM)
# ============================================================================

def _make_mock_llm_response(realm_role: str, service_id: str, role_name: str) -> str:
    """Return a well-formed LLM JSON response for a single role mapping."""
    return f"""
```explanation
Policy grants {realm_role} access to {role_name} on {service_id}.
```
```json
{{
  "service_role": "{role_name}",
  "real_roles_with_access": ["{realm_role}"]
}}
```
"""


def test_generate_policy_returns_expected_keys(config_file, mock_llm):
    """generate_policy result contains all documented keys."""
    mock_response = Mock()
    mock_response.content = _make_mock_llm_response(
        "developer", "github-tool", "github-tool-aud"
    )
    mock_llm.invoke.return_value = mock_response

    builder = ServicePolicyBuilder(
        service_id="github-tool",
        config_path=config_file,
        llm=mock_llm,
        verbose=False,
    )
    result = builder.generate_policy("Developers access public repos.")

    assert "yaml_output" in result
    assert "policy_structure" in result
    assert "parsed_scopes" in result
    assert "errors" in result
    assert "success" in result
    assert "retry_count" in result


def test_generate_policy_yaml_is_valid_yaml(config_file, mock_llm):
    """yaml_output in the result must be parseable YAML."""
    mock_response = Mock()
    mock_response.content = _make_mock_llm_response(
        "developer", "github-tool", "github-tool-aud"
    )
    mock_llm.invoke.return_value = mock_response

    builder = ServicePolicyBuilder(
        service_id="github-tool",
        config_path=config_file,
        llm=mock_llm,
        verbose=False,
    )
    result = builder.generate_policy("Developers get public GitHub access.")

    parsed = yaml.safe_load(result["yaml_output"])
    assert isinstance(parsed, dict)
    assert "policy" in parsed


def test_generate_policy_scoped_to_service_only(config_file, mock_llm):
    """All mappings in the generated policy belong to the specified service."""
    mock_response = Mock()
    mock_response.content = _make_mock_llm_response(
        "developer", "github-tool", "github-tool-aud"
    )
    mock_llm.invoke.return_value = mock_response

    builder = ServicePolicyBuilder(
        service_id="github-tool",
        config_path=config_file,
        llm=mock_llm,
        verbose=False,
    )
    result = builder.generate_policy("Developers get public GitHub access.")

    policy = result["policy_structure"].get("policy", {})
    for mappings in policy.values():
        for mapping in mappings:
            assert mapping["service"] == "github-tool", (
                f"Mapping for a different service leaked in: {mapping}"
            )


def test_invalid_role_triggers_validation_error(config_file, mock_llm):
    """A mapping with an unknown realm role is caught by validation."""
    mock_response = Mock()
    mock_response.content = """
```json
{
  "service_role": "github-tool-aud",
  "real_roles_with_access": ["nonexistent-realm-role"]
}
```
"""
    mock_llm.invoke.return_value = mock_response

    builder = ServicePolicyBuilder(
        service_id="github-tool",
        config_path=config_file,
        llm=mock_llm,
        verbose=False,
    )
    result = builder.generate_policy("Some policy description.")

    assert not result["success"], "Validation should fail for an unknown realm role"
    assert any(
        "nonexistent-realm-role" in str(err) for err in result["errors"]
    )


def test_output_scoped_even_when_llm_mentions_foreign_service_role(config_file, mock_llm):
    """
    The service/role in every output mapping always comes from the predefined
    service_roles list, not from the LLM JSON. Even if the LLM's service_role
    field names a role from a different service (demo-ui belongs to kagenti),
    the output must only contain github-tool roles and succeed.
    """
    mock_response = Mock()
    # LLM mentions demo-ui (a kagenti role) in its JSON service_role field.
    # That field is ignored; only real_roles_with_access matters.
    mock_response.content = """
```explanation
Mapping developer to demo-ui (wrong service - but service_role field is ignored).
```
```json
{
  "service_role": "demo-ui",
  "real_roles_with_access": ["developer"]
}
```
"""
    mock_llm.invoke.return_value = mock_response

    builder = ServicePolicyBuilder(
        service_id="github-tool",
        config_path=config_file,
        llm=mock_llm,
        verbose=False,
    )
    result = builder.generate_policy("Developers get GitHub access.")

    # The mapping is structurally valid: developer → github-tool roles
    assert result["success"], f"Unexpected errors: {result['errors']}"
    policy = result["policy_structure"].get("policy", {})
    all_services = {m["service"] for mappings in policy.values() for m in mappings}
    assert all_services == {"github-tool"}, (
        f"Foreign service leaked into output: {all_services}"
    )


# ============================================================================
# INTEGRATION TEST (requires LLM)
# ============================================================================

# @pytest.mark.skip(reason="Requires LLM access - run manually with a configured LLM")
def test_generate_service_policy_from_fixtures(fixtures_dir, config_file, policy_files, llm_instance, llm_model_name):
    """
    Integration test: generate a partial policy for each fixture using a real LLM.

    For every policy fixture the test:
    1. Reads the policy description from fixtures/policies/*.txt
    2. Loads the expected FULL policy from fixtures/expected/*.yaml
    3. Derives the expected PARTIAL policy by keeping only mappings for
       each target service defined in the fixture config
    4. Generates a partial policy with ServicePolicyBuilder
    5. Compares the result with the derived expected partial policy

    The test is parametrised over the four LLM models defined in llm_model_name.
    """
    if not policy_files:
        pytest.skip("No policy fixture files found")

    # Collect all service IDs from config
    config_data = yaml.safe_load((config_file).read_text())
    all_service_ids = [c["service_id"] for c in config_data.get("services", [])]

    failures = []

    for policy_file in policy_files:
        policy_description = policy_file.read_text().strip()
        expected_file = fixtures_dir / "expected" / f"{policy_file.stem}.yaml"

        if not expected_file.exists():
            failures.append(
                f"[{llm_model_name}] {policy_file.name}: missing expected file {expected_file}"
            )
            continue

        full_expected = normalize_policy_yaml(expected_file.read_text())

        for service_id in all_service_ids:
            expected_partial = filter_policy_to_service(full_expected, service_id)

            try:
                builder = ServicePolicyBuilder(
                    service_id=service_id,
                    config_path=config_file,
                    llm=llm_instance,
                    verbose=False,
                )
                result = builder.generate_policy(policy_description)

                if not result["success"]:
                    failures.append(
                        f"[{llm_model_name}] {policy_file.name} / {service_id}: "
                        f"generation failed: {result['errors']}"
                    )
                    continue

                generated_partial = normalize_policy_yaml(result["yaml_output"])
                match, diffs = compare_policies(generated_partial, expected_partial)

                if not match:
                    failures.append(
                        f"[{llm_model_name}] {policy_file.name} / {service_id}: "
                        "policy mismatch:\n"
                        + "\n".join(f"  - {d}" for d in diffs)
                    )

            except Exception as exc:
                failures.append(
                    f"[{llm_model_name}] {policy_file.name} / {service_id}: "
                    f"exception: {exc}"
                )

    if failures:
        pytest.fail(
            f"Service policy generation tests failed for model {llm_model_name}:\n\n"
            + "\n\n".join(failures)
        )


# ============================================================================
# FIXTURE SANITY CHECK
# ============================================================================

def test_fixture_files_exist(fixtures_dir):
    """Verify that fixture files are present and valid."""
    policies_dir = fixtures_dir / "policies"
    expected_dir = fixtures_dir / "expected"

    assert policies_dir.exists(), "fixtures/policies/ not found"
    assert expected_dir.exists(), "fixtures/expected/ not found"

    policy_files = list(policies_dir.glob("*.txt"))
    assert len(policy_files) > 0, "No .txt policy files found in fixtures/policies/"

    for policy_file in policy_files:
        expected_file = expected_dir / f"{policy_file.stem}.yaml"
        assert expected_file.exists(), (
            f"No expected output for {policy_file.name}: {expected_file}"
        )
        try:
            yaml.safe_load(expected_file.read_text())
        except yaml.YAMLError as exc:
            pytest.fail(f"Invalid YAML in {expected_file}: {exc}")
