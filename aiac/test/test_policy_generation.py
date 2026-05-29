"""
Integration tests for policy generation.

These tests generate policies from natural language descriptions and compare
them with expected YAML outputs. They require an LLM to be configured.

To run all tests (including integration tests):
    pytest tests/test_policy_generation.py

To skip integration tests (they require LLM access):
    pytest tests/test_policy_generation.py -m "not integration"

To run ONLY integration tests:
    pytest tests/test_policy_generation.py -m integration

To run the manually skipped test (test_generate_policy_from_fixtures):
    1. Ensure LLM is configured in aiac_agent/config/llm.env
    2. Remove or comment out the @pytest.mark.skip decorator on line 111
    3. Run: pytest tests/test_policy_generation.py::test_generate_policy_from_fixtures -v
"""

import pytest
import yaml
from pathlib import Path
from unittest.mock import Mock, patch

from full_policy_agent import PolicyBuilder
from config import create_llm


# Mark all tests in this module as integration tests
pytestmark = pytest.mark.integration


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
    policies_dir = fixtures_dir / "policies"
    return sorted(policies_dir.glob("*.txt"))


@pytest.fixture(params=[
    "claude-haiku",
    "gpt-nano",
    "gemini",
    "gpt-oss"
])
def llm_model_name(request):
    """Return model name for parametrized testing."""
    return request.param


@pytest.fixture
def llm_instance(llm_model_name):
    """Create LLM instance from YAML config."""
    return create_llm(model_name=llm_model_name, verbose=False)


def normalize_policy_yaml(yaml_content: str) -> dict:
    """
    Parse YAML and extract just the policy structure for comparison.
    
    This ignores comments and formatting differences, focusing only on
    the actual policy data structure.
    
    Args:
        yaml_content: YAML string content
        
    Returns:
        Dictionary containing the policy structure
    """
    data = yaml.safe_load(yaml_content)
    return data.get("policy", {})


def compare_policies(generated: dict, expected: dict) -> tuple[bool, list[str]]:
    """
    Compare two policy structures and return differences.

    Requires exact equality: every realm role and client-role mapping in
    *expected* must appear in *generated*, and *generated* must not contain
    any extra roles or mappings beyond what *expected* specifies.

    Args:
        generated: Generated policy structure
        expected: Expected policy structure

    Returns:
        Tuple of (policies_match: bool, differences: list[str])
    """
    differences = []

    generated_roles = set(generated.keys())
    expected_roles = set(expected.keys())

    missing_roles = expected_roles - generated_roles
    if missing_roles:
        differences.append(f"Missing realm roles: {missing_roles}")

    extra_roles = generated_roles - expected_roles
    if extra_roles:
        differences.append(f"Unexpected extra realm roles: {extra_roles}")

    for role in expected_roles & generated_roles:
        generated_mappings = generated[role]
        expected_mappings = expected[role]

        gen_set = {(m["client"], m["role"]) for m in generated_mappings}
        exp_set = {(m["client"], m["role"]) for m in expected_mappings}

        missing_mappings = exp_set - gen_set
        if missing_mappings:
            differences.append(
                f"Role '{role}' missing mappings: {missing_mappings}"
            )

        extra_mappings = gen_set - exp_set
        if extra_mappings:
            differences.append(
                f"Role '{role}' has unexpected extra mappings: {extra_mappings}"
            )

    return len(differences) == 0, differences


# @pytest.mark.skip(reason="Requires LLM access - run manually with configured LLM")
def test_generate_policy_from_fixtures(fixtures_dir, config_file, policy_files, llm_instance, llm_model_name):
    """
    Test policy generation for all fixture files with multiple LLM models.
    
    This test:
    1. Reads each policy description from fixtures/policies/*.txt
    2. Generates a policy using PolicyBuilder with the specified LLM
    3. Compares with expected YAML in fixtures/expected/*.yaml
    
    The test is parametrized to run with 4 different LLM models:
    - claude-haiku
    - gpt-nano
    - gemini
    - gpt-oss
    """
    if not policy_files:
        pytest.skip("No policy fixture files found")
    
    # Create PolicyBuilder instance with the parametrized LLM
    builder = PolicyBuilder(config_path=config_file, llm=llm_instance, verbose=False)
    
    # Use model name for error reporting
    model_name = llm_model_name
    
    failures = []
    
    for policy_file in policy_files:
        # Read policy description
        policy_description = policy_file.read_text().strip()
        
        # Determine expected output file
        expected_file = fixtures_dir / "expected" / f"{policy_file.stem}.yaml"
        
        if not expected_file.exists():
            failures.append(
                f"[{model_name}] {policy_file.name}: No expected output file found at {expected_file}"
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
                    f"[{model_name}] {policy_file.name}: Generation failed with errors: {result['errors']}"
                )
                continue
            
            # Parse generated YAML
            generated_policy = normalize_policy_yaml(result["yaml_output"])
            
            # Compare policies
            match, differences = compare_policies(generated_policy, expected_policy)
            
            if not match:
                failures.append(
                    f"[{model_name}] {policy_file.name}: Generated policy doesn't match expected:\n"
                    + "\n".join(f"  - {diff}" for diff in differences)
                )
        
        except Exception as e:
            failures.append(f"[{model_name}] {policy_file.name}: Exception during generation: {e}")
    
    # Report all failures at once
    if failures:
        pytest.fail(
            f"Policy generation tests failed for model {model_name}:\n\n" + "\n\n".join(failures)
        )


def test_policy_builder_can_generate_yaml_from_structure(config_file):
    """
    Test that PolicyBuilder can generate YAML from a policy structure.
    
    This test bypasses LLM calls and directly tests the YAML generation logic.
    """
    from full_policy_agent.graph import _generate_yaml
    from full_policy_agent.state import PolicyState
    
    # Create a valid policy state with all required PolicyState fields
    state: PolicyState = {
        "description": "Test policy description",
        "explanation": "Test explanation",
        "policy_structure": {
            "policy": {
                "developer": [
                    {"client": "kagenti", "role": "demo-ui"},
                    {"client": "github-tool", "role": "github-full-access"}
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


def test_invalid_policy_triggers_validation_errors(config_file):
    """
    Test that invalid policies are caught by validation.
    
    This test uses a mock LLM that returns an invalid policy structure
    to verify that validation catches errors.
    """
    # Create a mock LLM that returns an invalid policy (unknown role)
    mock_llm = Mock()
    mock_response = Mock()
    mock_response.content = """
    ```json
    [
        {
            "role": "unknown-role",
            "client_roles": [
                {"client": "kagenti", "role": "demo-ui"}
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


def test_policy_builder_initialization(config_file):
    """Test that PolicyBuilder initializes correctly with config file."""
    # Create a mock LLM to avoid requiring actual LLM configuration
    mock_llm = Mock()
    
    builder = PolicyBuilder(config_path=config_file, llm=mock_llm, verbose=False)
    
    # Verify configuration was loaded
    # realm_roles are now dicts with 'name' and 'description'
    realm_role_names = [role['name'] for role in builder.realm_roles]
    assert realm_role_names == ["developer", "tech-support", "sales"]
    
    # Verify clients were loaded
    assert "kagenti" in builder.client_roles_map
    assert "github-tool" in builder.client_roles_map
    assert "spiffe://localtest.me/ns/team1/sa/git-issue-agent" in builder.client_roles_map
    
    # Verify client roles are dicts with 'name' and 'description'
    kagenti_roles = builder.client_roles_map["kagenti"]
    assert len(kagenti_roles) > 0
    assert all(isinstance(role, dict) and 'name' in role for role in kagenti_roles)


def test_fixture_files_exist(fixtures_dir):
    """Verify that fixture files are present and properly structured."""
    policies_dir = fixtures_dir / "policies"
    expected_dir = fixtures_dir / "expected"
    
    assert policies_dir.exists(), "Policies directory not found"
    assert expected_dir.exists(), "Expected directory not found"
    
    policy_files = list(policies_dir.glob("*.txt"))
    assert len(policy_files) > 0, "No policy fixture files found"
    
    # Check that each policy file has a corresponding expected file
    for policy_file in policy_files:
        expected_file = expected_dir / f"{policy_file.stem}.yaml"
        assert expected_file.exists(), (
            f"Missing expected output for {policy_file.name}: {expected_file}"
        )
        
        # Verify expected file is valid YAML
        try:
            yaml.safe_load(expected_file.read_text())
        except yaml.YAMLError as e:
            pytest.fail(f"Invalid YAML in {expected_file}: {e}")

