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

import os
import pytest
import yaml
from pathlib import Path
from unittest.mock import Mock

from aiac.pdp.policy.models import PolicyObjectModel, Rule, ServiceObjectModel
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
    """Create LLM instance from YAML config, skip if the endpoint is unreachable."""
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

    os.environ["AIAC_PDP_CONFIG_PATH"] = str(config_file)

    # Create PolicyBuilder instance with the parametrized LLM
    builder = PolicyBuilder(llm=llm_instance, verbose=False)

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
            policy = builder.generate_policy(policy_description)

            # Get YAML output from builder (generated on-demand)
            yaml_output = builder.get_yaml_output()

            # Parse generated YAML
            generated_policy = normalize_policy_yaml(yaml_output)

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

def test_save_policy_creates_yaml_file(tmp_path):
    """save_policy writes valid YAML to the specified path."""
    from aiac.pdp.policy.builders.yaml import save_policy_yaml
    from aiac.pdp.policy.models import PolicyObjectModel
    from aiac.pdp.library.configuration.models import Service

    svc = Service(id="kagenti", name = "kagenti", serviceId="kagenti", enabled=True, type="Agent")
    # policy={"developer": [Priviledge(name="demo-ui", services=[svc])]}

    policy = PolicyObjectModel(
        name="Test policy",
        policy={svc.id: ServiceObjectModel(service_type="Agent",inbound_rules=[Rule(role="developer", scope="demo-ui")])})
                        
    output_file = tmp_path / "policy.yaml"
    save_policy_yaml(policy, str(output_file))

    assert output_file.exists()
    content = output_file.read_text()
    assert "policy:" in content
    assert "developer:" in content
    assert "kagenti" in content
    assert "demo-ui" in content
    assert "# Access Control Policy" in content
    assert "Test policy" in content


def test_save_policy_rego_creates_files(tmp_path, config_file):
    """save_policy_rego writes realm_roles and default Rego files to the directory."""
    from aiac.pdp.policy.builders.rego import save_policy_rego
    from aiac.pdp.policy.models import PolicyObjectModel
    from aiac.pdp.library.configuration.models import Service
    import os

    os.environ["AIAC_PDP_CONFIG_PATH"] = str(config_file)

    svc_kagenti = Service(id="kagenti", serviceId="kagenti", enabled=True, type="Agent")
    svc_github = Service(id="github-tool", serviceId="github-tool", enabled=True, type="Tool")
    policy = PolicyObjectModel(
        name="Test policy",
        policy={svc_kagenti.id: ServiceObjectModel(service_type="Agent", inbound_rules=[Rule(role="developer", scope="demo-ui")]),
                svc_github.id: ServiceObjectModel(service_type="Tool", inbound_rules=[Rule(role="developer", scope="github-full-access")])})

    save_policy_rego(policy, str(tmp_path), realm="demo")

    assert (tmp_path / "realm_roles.rego").exists()
    assert (tmp_path / "default_inbound.rego").exists()
    assert (tmp_path / "default_outbound.rego").exists()
    # One file per service referenced in the policy
    assert (tmp_path / "generated_policy_kagenti.rego").exists()
    assert (tmp_path / "generated_policy_github-tool.rego").exists()

    inbound_content = (tmp_path / "default_inbound.rego").read_text()
    assert "default allow := false" in inbound_content
    outbound_content = (tmp_path / "default_outbound.rego").read_text()
    assert "default allow := false" in outbound_content


def test_policy_builder_can_generate_yaml_from_structure(config_file):
    """PolicyBuilder can generate YAML from a policy structure (bypasses LLM)."""
    from aiac.pdp.policy.builders.yaml import _generate_yaml_output

    # Create a valid policy structure
    policy_structure = {
        "policy": {
            "developer": [
                {"service": "kagenti", "privilege": "demo-ui"},
                {"service": "github-tool", "privilege": "github-full-access"}
            ]
        }
    }
    
    policy = PolicyObjectModel(
        name="Test policy description",
        policy={"kagenti": ServiceObjectModel(service_type="Agent", inbound_rules=[Rule(role="developer", scope="demo-ui")]),
                "github-tool": ServiceObjectModel(service_type="Tool", inbound_rules=[Rule(role="developer", scope="github-full-access")])})


    # Generate YAML
    yaml_output = _generate_yaml_output(policy)

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
    os.environ["AIAC_PDP_CONFIG_PATH"] = str(config_file)

    builder = PolicyBuilder(llm=mock_llm, verbose=False)

    with pytest.raises(ValueError, match="unknown-role"):
        builder.generate_policy("Invalid policy description")


def test_policy_builder_initialization(config_file, mock_llm):
    """PolicyBuilder initializes correctly with config file."""
    os.environ["AIAC_PDP_CONFIG_PATH"] = str(config_file)

    builder = PolicyBuilder(llm=mock_llm, verbose=False)

    # Verify configuration was loaded
    # realm_roles are now dicts with 'name' and 'description'
    realm_role_names = [role['name'] for role in builder.realm_roles]
    assert "developer" in realm_role_names
    assert "tech-support" in realm_role_names
    assert "sales" in realm_role_names

    # Verify services were loaded
    assert "kagenti" in builder.privileges_map
    assert "github-tool" in builder.privileges_map
    assert "spiffe://localtest.me/ns/team1/sa/git-issue-agent" in builder.privileges_map

    # privileges_map values are {service_type, scopes} — service_type is per-service, not per-scope
    kagenti_info = builder.privileges_map["kagenti"]
    assert isinstance(kagenti_info, dict)
    assert "service_type" in kagenti_info
    assert "scopes" in kagenti_info
    assert len(kagenti_info["scopes"]) > 0
    assert all(isinstance(r, dict) and 'name' in r for r in kagenti_info["scopes"])
    # service_type must not appear inside individual scope entries
    assert all('service_type' not in r for r in kagenti_info["scopes"])


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

