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

from aiac.idp.configuration.models import Role, Scope
from aiac.agent.onboarding.policy.full_policy_agent import PolicyBuilder
from aiac.policy.model.models import PolicyRule
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
    "gpt-5-mini",
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

def _make_rule(role_name: str, scope_name: str, service_id: str) -> PolicyRule:
    role = Role(id=role_name, name=role_name, description="", composite=False)
    scope = Scope(id=scope_name, name=scope_name)
    return PolicyRule(role=role, scope=scope)


def _policy_to_role_map(rules: list[PolicyRule]) -> dict[str, set[str]]:
    """Extract {role_name: {scope_names}} from a PolicyObjectModel."""
    result: dict[str, set[str]] = {}
    for rule in rules:
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
        differences.append(f"Missing role: '{role}'")
    for role in generated_roles - expected_roles:
        differences.append(f"Unexpected extra role: '{role}'")

    for role in expected_roles & generated_roles:
        gen_set = generated[role]
        exp_set = expected[role]
        for priv in exp_set - gen_set:
            differences.append(f"Role '{role}' missing privilege: {priv}")
        for priv in gen_set - exp_set:
            differences.append(f"Role '{role}' has unexpected extra privilege: {priv}")

    return len(differences) == 0, differences


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
