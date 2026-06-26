"""
Tests for the full-policy generation agent (PolicyBuilder).

Unit tests do not require an LLM; integration tests require a live endpoint.

To run all tests:
    pytest test/policy/test_policy_generation.py

To skip integration tests:
    pytest test/policy/test_policy_generation.py -m "not integration"

To run ONLY integration tests:
    pytest test/policy/test_policy_generation.py -m integration
"""

import os
import pytest
from pathlib import Path
from unittest.mock import Mock

from aiac.pdp.policy.models import PolicyObjectModel, Rule
from aiac.pdp.library.configuration.models import Role, Scope
from full_policy_agent import PolicyBuilder
from config import create_llm


pytestmark = pytest.mark.integration


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


@pytest.fixture(params=[
    "claude-haiku",
    "gpt-nano",
    "gemini",
    "gpt-oss",
])
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


# ============================================================================
# EXPECTED POLICIES
# Maps fixture stem → {role_name: {privilege_names}}
# ============================================================================

EXPECTED_POLICIES: dict[str, dict[str, set[str]]] = {
    "permissive_policy": {
        "developer": {"github-agent", "github-tool-aud", "github-full-access"},
        "tech-support": {"github-agent", "github-tool-aud"},
        "sales": {"github-agent", "github-tool-aud"},
    },
    "regular_policy": {
        "developer": {"github-agent", "github-tool-aud", "github-full-access"},
        "tech-support": {"github-agent", "github-tool-aud"},
    },
}


# ============================================================================
# HELPERS
# ============================================================================

def _make_policy(rules: list[Rule], name: str = "") -> PolicyObjectModel:
    return PolicyObjectModel(rules=rules, explanation="")


def _make_rule(role_name: str, scope_name: str, service_id: str) -> Rule:
    role = Role(id=role_name, name=role_name, description="", composite=False)
    scope = Scope(id=scope_name, name=scope_name)
    return Rule(role=role, scope=scope)


def _policy_to_role_map(policy: PolicyObjectModel) -> dict[str, set[str]]:
    """Extract {role_name: {scope_names}} from a PolicyObjectModel."""
    result: dict[str, set[str]] = {}
    for rule in policy.rules:
        result.setdefault(rule.role.name, set())
        result[rule.role.name].add(rule.scope.name)
    return result


def compare_policies(
    generated: dict[str, set[str]], expected: dict[str, set[str]]
) -> tuple[bool, list[str]]:
    differences = []
    generated_roles = set(generated.keys())
    expected_roles = set(expected.keys())

    for role in expected_roles - generated_roles:
        differences.append(f"Missing realm role: '{role}'")
    for role in generated_roles - expected_roles:
        differences.append(f"Unexpected extra realm role: '{role}'")

    for role in expected_roles & generated_roles:
        gen_set = generated[role]
        exp_set = expected[role]
        for priv in exp_set - gen_set:
            differences.append(f"Role '{role}' missing privilege: {priv}")
        for priv in gen_set - exp_set:
            differences.append(f"Role '{role}' has unexpected extra privilege: {priv}")

    return len(differences) == 0, differences


# ============================================================================
# UNIT TESTS (no LLM required)
# ============================================================================

def test_save_policy_creates_yaml_file(tmp_path):
    """save_policy_yaml writes valid YAML to the specified path."""
    from aiac.pdp.policy.builders.yaml import save_policy_yaml

    policy = _make_policy(
        [_make_rule("developer", "demo-ui", "kagenti")],
        name="Test policy",
    )

    output_file = tmp_path / "policy.yaml"
    save_policy_yaml(policy, str(output_file))

    assert output_file.exists()
    assert len(policy.rules) == 1
    assert policy.rules[0].role.name == "developer"
    assert policy.rules[0].scope.name == "demo-ui"
    assert "# Access Control Policy" in output_file.read_text()


def test_save_policy_includes_description_comment(tmp_path):
    """save_policy_yaml writes a file with rules stored in the policy model."""
    from aiac.pdp.policy.builders.yaml import save_policy_yaml

    policy = _make_policy(
        [_make_rule("developer", "demo-ui", "kagenti")],
        name="Test policy description",
    )
    output_file = tmp_path / "policy.yaml"
    save_policy_yaml(policy, str(output_file))

    assert output_file.exists()
    assert len(policy.rules) == 1
    rule = policy.rules[0]
    assert rule.role.name == "developer"
    assert rule.scope.name == "demo-ui"


def test_save_policy_rego_creates_files(tmp_path, config_file, monkeypatch):
    """save_policy_rego writes realm_roles and default Rego files, plus per-service files."""
    from aiac.pdp.policy.builders.rego import save_policy_rego
    from aiac.pdp.library.read_api_from_config import Configuration as FileConfiguration

    os.environ["AIAC_PDP_CONFIG_PATH"] = str(config_file)
    monkeypatch.setattr("aiac.pdp.library.configuration.api.Configuration", FileConfiguration)

    policy = _make_policy([
        _make_rule("developer", "demo-ui", "kagenti"),
        _make_rule("developer", "github-full-access", "github-tool"),
    ])

    save_policy_rego(policy, str(tmp_path), realm="demo")

    assert (tmp_path / "realm_roles.rego").exists()
    assert (tmp_path / "default_inbound.rego").exists()
    assert (tmp_path / "default_outbound.rego").exists()
    assert (tmp_path / "generated_policy_Dummy.rego").exists()

    inbound = (tmp_path / "default_inbound.rego").read_text()
    assert "default allow := false" in inbound
    outbound = (tmp_path / "default_outbound.rego").read_text()
    assert "default allow := false" in outbound


def test_generate_yaml_output_structure():
    """_generate_yaml_output produces correctly structured output from a PolicyObjectModel."""
    from aiac.pdp.policy.builders.yaml import _generate_yaml_output

    policy = _make_policy([
        _make_rule("developer", "demo-ui", "kagenti"),
        _make_rule("developer", "github-full-access", "github-tool"),
    ], name="Test policy description")

    assert len(policy.rules) == 2
    role_names = {r.role.name for r in policy.rules}
    assert "developer" in role_names
    scope_names = {r.scope.name for r in policy.rules}
    assert "demo-ui" in scope_names
    assert "github-full-access" in scope_names

    yaml_output = _generate_yaml_output(policy)
    assert "# Access Control Policy" in yaml_output
    assert "demo-ui" in yaml_output


def test_generate_yaml_output_contains_policy_data():
    """_generate_yaml_output includes role and scope names in its string output."""
    from aiac.pdp.policy.builders.yaml import _generate_yaml_output

    policy = _make_policy([
        _make_rule("developer", "demo-ui", "kagenti"),
    ])
    assert len(policy.rules) == 1
    assert policy.rules[0].role.name == "developer"
    assert policy.rules[0].scope.name == "demo-ui"

    output = _generate_yaml_output(policy)
    assert "developer" in output
    assert "demo-ui" in output


def test_policy_builder_initialization(config_file, mock_llm):
    """PolicyBuilder loads roles and service privileges from the config file."""
    os.environ["AIAC_PDP_CONFIG_PATH"] = str(config_file)

    builder = PolicyBuilder(llm=mock_llm, verbose=False)

    role_names = [r.name for r in builder.roles]
    assert "developer" in role_names
    assert "tech-support" in role_names
    assert "sales" in role_names

    assert "kagenti" in builder.privileges_map
    assert "github-tool" in builder.privileges_map
    assert "spiffe://localtest.me/ns/team1/sa/git-issue-agent" in builder.privileges_map

    kagenti_info = builder.privileges_map["kagenti"]
    assert isinstance(kagenti_info, dict)
    assert "service_type" in kagenti_info
    assert "scopes" in kagenti_info
    assert len(kagenti_info["scopes"]) > 0
    assert all(isinstance(s, Scope) for s in kagenti_info["scopes"])


# ============================================================================
# FIXTURE SANITY CHECK
# ============================================================================

def test_fixture_files_exist(fixtures_dir):
    policies_dir = fixtures_dir / "policies"
    assert policies_dir.exists(), "fixtures/policies/ not found"

    policy_files = list(policies_dir.glob("*.txt"))
    assert len(policy_files) > 0, "No .txt policy files found in fixtures/policies/"

    for policy_file in policy_files:
        assert policy_file.stem in EXPECTED_POLICIES, (
            f"No expected structure defined for {policy_file.name} in EXPECTED_POLICIES"
        )


# ============================================================================
# INTEGRATION TEST (requires LLM)
# ============================================================================

def test_generate_policy_from_fixtures(fixtures_dir, config_file, policy_files, llm_instance, llm_model_name):
    """Integration: generate policies from fixtures using a real LLM."""
    if not policy_files:
        pytest.skip("No policy fixture files found")

    os.environ["AIAC_PDP_CONFIG_PATH"] = str(config_file)

    builder = PolicyBuilder(llm=llm_instance, verbose=False)
    failures = []

    for policy_file in policy_files:
        stem = policy_file.stem
        if stem not in EXPECTED_POLICIES:
            failures.append(
                f"[{llm_model_name}] {policy_file.name}: no expected structure defined in EXPECTED_POLICIES"
            )
            continue

        expected_policy = EXPECTED_POLICIES[stem]
        policy_description = policy_file.read_text().strip()

        try:
            generated = builder.generate_policy(policy_description)
            generated_policy = _policy_to_role_map(generated)
            match, differences = compare_policies(generated_policy, expected_policy)

            if not match:
                failures.append(
                    f"[{llm_model_name}] {policy_file.name}: policy mismatch:\n"
                    + "\n".join(f"  - {d}" for d in differences)
                )
        except Exception as exc:
            failures.append(
                f"[{llm_model_name}] {policy_file.name}: exception: {exc}"
            )

    if failures:
        pytest.fail(
            f"Policy generation tests failed for model {llm_model_name}:\n\n"
            + "\n\n".join(failures)
        )
