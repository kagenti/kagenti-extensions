"""Live-LLM acceptance tests for aiac.agent.policy_rules_builder.graph.

Unlike test_graph.py (which mocks the LLM at the graph._structured_call seam), this
suite runs the **real** LLM end-to-end through the Policy Rules Builder and asserts that
the emitted ``list[PolicyRule]`` matches the policy text. Only the *inputs* are mocked —
role/scope **descriptions** are set inline and the **policy source** is stubbed at
graph.get_policy_source — so the suite needs **no Kubernetes and no Keycloak**, only an
LLM endpoint. The ``_structured_call`` seam is deliberately **left live**: the whole
point is to exercise the real proposer/auditor prompts, so a prompt-engineering
regression (an over-grant, a missed deny, a hallucinated deny, a wrong effect) fails a
fixture here where the mocked suite cannot see it.

Gating: the module is marked **both** ``integration`` and ``llm``. It is ``integration``
because it calls a real LLM endpoint — that is exactly what the ``integration`` marker
means — so the routine unit run (``-m "not integration"``) deselects it and its collected
count is unchanged by this suite. It is additionally ``llm`` so it can be selected on its
own (``-m llm``) without a cluster or Keycloak, unlike the cluster-bound integration
suite. Every test also first calls
``require_env_or_skip("LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY")`` so it **skips cleanly**
(never crashes, never false-passes) when the endpoint is not configured. Run it opt-in
with ``-m llm`` (env sourced).

Each fixture asserts **exact set equality** of the emitted ``{(counterpart.name, effect)}``
pairs against a hand-verified expected set — a subset check would let over-/under-grants
pass. Expected sets are derived by hand from the SCENARIO-layer deny triggers documented
in ``docs/handoffs/03/04-*.md`` and ``prompts.py`` (_DENY_RULES): explicit prohibition
("must not" / "read-only") -> DENY; a prohibition stated only in a description -> DENY;
"only …" -> derived DENY complement over the rest of the candidate set.
"""

from unittest.mock import patch

import pytest

from aiac.agent.policy_rules_builder.graph import build_role_rules, build_scope_rules
from aiac.idp.configuration.models import Role, Scope
from aiac.policy.model.models import PolicyRule, RuleEffect
from test.integration.launcher import require_env_or_skip

pytestmark = [pytest.mark.integration, pytest.mark.llm]

ALLOW = RuleEffect.ALLOW
DENY = RuleEffect.DENY


# --------------------------------------------------------------------------- #
# Harness                                                                     #
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _require_llm_env():
    """Skip the whole suite cleanly unless a real LLM endpoint is configured. Autouse so
    every fixture is gated without repeating the call; runs before the patched build."""
    require_env_or_skip("LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY")


class _Source:
    """Stub PolicySource whose fetch() returns a fixed scenario policy string (mirrors the
    _Source in test_graph.py). Patched in at graph.get_policy_source per fixture."""

    def __init__(self, text: str):
        self.text = text

    def fetch(self) -> str:
        return self.text


def _role(id: str, name: str, description: str) -> Role:
    return Role(id=id, name=name, description=description, composite=False, childRoles=[])


def _scope(id: str, name: str, description: str) -> Scope:
    return Scope(id=id, name=name, description=description)


def _role_rules(policy: str, role: Role, scopes: list[Scope]) -> list[PolicyRule]:
    """Run build_role_rules against the real LLM with `policy` as the scenario text."""
    with patch("aiac.agent.policy_rules_builder.graph.get_policy_source", return_value=_Source(policy)):
        return build_role_rules(role, scopes)


def _scope_rules(policy: str, roles: list[Role], scope: Scope) -> list[PolicyRule]:
    """Run build_scope_rules against the real LLM with `policy` as the scenario text."""
    with patch("aiac.agent.policy_rules_builder.graph.get_policy_source", return_value=_Source(policy)):
        return build_scope_rules(roles, scope)


def _scope_effects(rules: list[PolicyRule]) -> set[tuple[str, RuleEffect]]:
    """Emitted (scope.name, effect) pairs — the counterpart for the role direction."""
    return {(r.scope.name, r.effect) for r in rules}


def _role_effects(rules: list[PolicyRule]) -> set[tuple[str, RuleEffect]]:
    """Emitted (role.name, effect) pairs — the counterpart for the scope direction."""
    return {(r.role.name, r.effect) for r in rules}


# --------------------------------------------------------------------------- #
# Slice 1 (tracer) — allow-only, build_role_rules. A purely permissive policy   #
# grants both source scopes to the developer and prohibits nothing: exactly two #
# ALLOWs, no DENY. Proves the seam wiring + marker gating before any deny logic. #
# --------------------------------------------------------------------------- #
def test_allow_only_role_direction():
    developer = _role(
        "r-dev", "developer", "A software developer who works on the source code repository."
    )
    source_read = _scope("s-read", "source-read", "Read source code from the repository.")
    source_write = _scope("s-write", "source-write", "Write and modify source code in the repository.")

    policy = "Developers may read and write the source code repository."

    rules = _role_rules(policy, developer, [source_read, source_write])

    assert _scope_effects(rules) == {
        ("source-read", ALLOW),
        ("source-write", ALLOW),
    }


# --------------------------------------------------------------------------- #
# Slice 2 — allow-only, build_scope_rules (symmetric, opposite direction). The  #
# scope is focal, roles are candidates. The policy connects only `operator` to  #
# deploy; `developer` is a silent non-grant (neither the policy nor its         #
# description connects it to deploying) — a non-grant, NOT a DENY. Locks the     #
# symmetric path: exactly one ALLOW, no DENY.                                   #
# --------------------------------------------------------------------------- #
def test_allow_only_scope_direction():
    deploy = _scope("s-deploy", "deploy", "Deploy the application to production.")
    operator = _role(
        "r-ops",
        "operator",
        "An operations engineer responsible for deploying and running the application in production.",
    )
    developer = _role("r-dev", "developer", "A software developer who writes source code.")

    policy = "Operators may deploy the application to production."

    rules = _scope_rules(policy, [operator, developer], deploy)

    assert _role_effects(rules) == {("operator", ALLOW)}


# --------------------------------------------------------------------------- #
# Slice 3 — direct-prohibition deny (role direction). "read the source but must  #
# not write to it": the read-only prohibition in the SCENARIO policy records an  #
# explicit DENY on the write scope (rule 5), alongside the read ALLOW.          #
# --------------------------------------------------------------------------- #
def test_direct_prohibition_deny():
    developer = _role(
        "r-dev", "developer", "A software developer who works on the source code repository."
    )
    source_read = _scope("s-read", "source-read", "Read source code from the repository.")
    source_write = _scope("s-write", "source-write", "Write and modify source code in the repository.")

    policy = "Developers may read the source code repository, but must not write to it."

    rules = _role_rules(policy, developer, [source_read, source_write])

    assert _scope_effects(rules) == {
        ("source-read", ALLOW),
        ("source-write", DENY),
    }


# --------------------------------------------------------------------------- #
# Slice 4 — description-driven deny. The prohibition lives ONLY in the focal      #
# role's description ("does not manage the issue tracker"); the scenario policy  #
# is silent on issues. The DENY must still appear — descriptions are read        #
# symmetrically for grants and prohibitions — alongside the source ALLOW.        #
# --------------------------------------------------------------------------- #
def test_description_driven_deny():
    source_agent = _role(
        "r-agent",
        "source-agent",
        "An agent that manages the source code repository. It does not manage the issue tracker.",
    )
    source_manage = _scope("s-src", "source-manage", "Manage the source code repository (read and write).")
    issues_manage = _scope("s-iss", "issues-manage", "Manage the issue tracker.")

    policy = "The source-agent manages the source code repository."

    rules = _role_rules(policy, source_agent, [source_manage, issues_manage])

    assert _scope_effects(rules) == {
        ("source-manage", ALLOW),
        ("issues-manage", DENY),
    }


# --------------------------------------------------------------------------- #
# Slice 5 — exclusivity complement (highest-signal). "may ONLY access source"    #
# closes the set: source is granted, and the builder derives a DENY on EVERY     #
# other candidate (issues, deploy). A missed complement is exactly the durable-  #
# prohibition hole DENY exists to close.                                        #
# --------------------------------------------------------------------------- #
def test_exclusivity_derives_complement():
    developer = _role("r-dev", "developer", "A software developer.")
    source = _scope("s-src", "source", "Access the source code repository.")
    issues = _scope("s-iss", "issues", "Access the issue tracker.")
    deploy = _scope("s-dep", "deploy", "Deploy the application to production.")

    policy = "Developers may only access the source code repository."

    rules = _role_rules(policy, developer, [source, issues, deploy])

    assert _scope_effects(rules) == {
        ("source", ALLOW),
        ("issues", DENY),
        ("deploy", DENY),
    }
