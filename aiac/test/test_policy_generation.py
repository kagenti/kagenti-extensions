"""
Integration tests for policy generation.

These tests generate policies from natural language descriptions and compare
them with expected YAML outputs. They require an LLM to be configured.

To run all tests:
    pytest test/test_policy_generation.py

To skip integration tests (require LLM access):
    pytest test/test_policy_generation.py -m "not integration"

To run ONLY integration tests:
    pytest test/test_policy_generation.py -m integration

To run the LLM-backed fixture test:
    1. Ensure LLM is configured in config/llm.env
    2. Remove the @pytest.mark.skip decorator on test_generate_policy_from_fixtures
    3. Run: pytest test/test_policy_generation.py::test_generate_policy_from_fixtures -v
"""

import pytest
import yaml
from pathlib import Path
from unittest.mock import Mock

from full_policy_agent import PolicyBuilder
from config import create_llm


# Mark all tests in this module as integration tests
pytestmark = pytest.mark.integration


# ============================================================================
# FIXTURES
# ============================================================================

@pytest.fixture
def fixtures_dir():
    """Return path to test fixtures directory."""
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def config_file():
    """Return path to the main config.yaml file."""
    return Path(__file__).parent / "fixtures" / "config.yaml"


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
# INTEGRATION TEST (requires LLM)
# ============================================================================

# @pytest.mark.skip(reason="Requires LLM access - run manually with a configured LLM")
def test_generate_policy_from_fixtures(fixtures_dir, config_file, policy_files, llm_instance, llm_model_name):
    """
    Integration test: generate policies from fixtures using a real LLM.

    For every policy fixture the test:
    1. Reads the policy description from fixtures/policies/*.txt
    2. Generates a policy using PolicyBuilder with the specified LLM
    3. Compares with expected YAML in fixtures/expected/*.yaml

    The test is parametrised over the four LLM models defined in llm_model_name.
    """
    if not policy_files:
        pytest.skip("No policy fixture files found")

    # Create PolicyBuilder instance with the parametrized LLM
    builder = PolicyBuilder(config_path=config_file, llm=llm_instance, verbose=False)

    failures = []

    for policy_file in policy_files:
        # Read policy description
        policy_description = policy_file.read_text().strip()

        # Determine expected output file
        expected_file = fixtures_dir / "expected" / f"{policy_file.stem}.yaml"

        if not expected_file.exists():
            failures.append(
                f"[{llm_model_name}] {policy_file.name}: missing expected file {expected_file}"
            )
            continue

        # Read expected output
        expected_yaml = expected_file.read_text()
        expected_policy = normalize_policy_yaml(expected_yaml)

        # Generate policy
        try:
            result = builder.generate_policy(policy_description)

            if not result["success"]:
                failures.append(
                    f"[{llm_model_name}] {policy_file.name}: "
                    f"generation failed: {result['errors']}"
                )
                continue

            # Parse generated YAML
            generated_policy = normalize_policy_yaml(result["yaml_output"])

            # Compare policies
            match, differences = compare_policies(generated_policy, expected_policy)

            if not match:
                failures.append(
                    f"[{llm_model_name}] {policy_file.name}: "
                    "policy mismatch:\n"
                    + "\n".join(f"  - {diff}" for diff in differences)
                )

        except Exception as exc:
            failures.append(
                f"[{llm_model_name}] {policy_file.name}: "
                f"exception: {exc}"
            )

    # Report all failures at once
    if failures:
        pytest.fail(
            f"Policy generation tests failed for model {llm_model_name}:\n\n"
            + "\n\n".join(failures)
        )


# ============================================================================
# UNIT TESTS (no LLM required)
# ============================================================================

def test_policy_builder_can_generate_yaml_from_structure(config_file):
    """PolicyBuilder can generate YAML from a policy structure (bypasses LLM)."""
    from full_policy_agent.graph import _generate_yaml
    from full_policy_agent.state import PolicyState

    # Create a valid policy state with all required fields
    state: PolicyState = {
        "description": "Test policy description",
        "explanation": "Test explanation",
        "policy_structure": {
            "policy": {
                "developer": [
                    {"service": "kagenti", "role": "demo-ui"},
                    {"service": "github-tool", "role": "github-full-access"}
                ]
            }
        },
        "parsed_scopes": [],
        "yaml_output": "",
        "messages": [],
        "errors": [],
        "retry_count": 0,
        "validation_passed": True
    }

    # Generate YAML
    result_state = _generate_yaml(state)

    # Verify YAML was generated
    assert "yaml_output" in result_state
    yaml_output = result_state["yaml_output"]

    # Verify YAML contains expected content
    assert "policy:" in yaml_output
    assert "developer:" in yaml_output
    assert "kagenti" in yaml_output
    assert "demo-ui" in yaml_output
    assert "# Access Control Policy" in yaml_output
    assert "# Original Policy Description:" in yaml_output
    assert "Test policy description" in yaml_output


def test_invalid_policy_triggers_validation_errors(config_file, mock_llm):
    """Invalid policies are caught by validation (uses mock LLM)."""
    # Mock LLM returns an invalid policy (unknown role)
    mock_response = Mock()
    mock_response.content = """
    ```json
    [
        {
            "role": "unknown-role",
            "privileges": [
                {"service": "kagenti", "privilege": "demo-ui"}
            ]
        }
    ]
    ```
    """
    mock_llm.invoke.return_value = mock_response

    # Create PolicyBuilder with mock LLM
    builder = PolicyBuilder(config_path=config_file, llm=mock_llm, verbose=False)

    # Generate policy
    result = builder.generate_policy("Invalid policy description")

    # Verify validation caught the error
    assert not result["success"], "Expected validation to fail for unknown role"
    assert len(result["errors"]) > 0
    assert any("unknown-role" in str(err).lower() for err in result["errors"])


def test_policy_builder_initialization(config_file, mock_llm):
    """PolicyBuilder initializes correctly with config file."""
    builder = PolicyBuilder(config_path=config_file, llm=mock_llm, verbose=False)

    # Verify configuration was loaded
    # realm_roles are now dicts with 'name' and 'description'
    realm_role_names = [role['name'] for role in builder.realm_roles]
    assert realm_role_names == ["developer", "tech-support", "sales"]

    # Verify services were loaded
    assert "kagenti" in builder.privileges_map
    assert "github-tool" in builder.privileges_map
    assert "spiffe://localtest.me/ns/team1/sa/git-issue-agent" in builder.privileges_map

    # Verify privileges are dicts with 'name' and 'description'
    kagenti_privileges = builder.privileges_map["kagenti"]
    assert len(kagenti_privileges) > 0
    assert all(isinstance(priv, dict) and 'name' in priv for priv in kagenti_privileges)


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

    # Check that each policy file has a corresponding expected file
    for policy_file in policy_files:
        expected_file = expected_dir / f"{policy_file.stem}.yaml"
        assert expected_file.exists(), (
            f"No expected output for {policy_file.name}: {expected_file}"
        )

        # Verify expected file is valid YAML
        try:
            yaml.safe_load(expected_file.read_text())
        except yaml.YAMLError as exc:
            pytest.fail(f"Invalid YAML in {expected_file}: {exc}")

