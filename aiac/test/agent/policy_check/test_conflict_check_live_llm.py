"""Live-LLM planted-fixture suite for the Policy Conflict Check diagnostic (feature #154, task #160).

Mirrors ``test/agent/policy_rules_builder/test_graph_live_llm.py``: it runs the **real** LLM
end-to-end through the read-only conflict-check survey (``check_policy_conflicts``) and asserts
**structural** properties of the emitted ``ConflictReport`` -- never exact quote strings or
explanation wording (model nondeterminism; convergence on subtle prose is known-fragile).

Only the IdP catalog is stubbed -- the ``focal_entities._config`` seam ``check_policy_conflicts``
reuses for BOTH the ``service_type`` lookup and ``resolve_focal_entities`` (a single patch point,
preserving the 502/404 boundary). ``graph._structured_call`` is deliberately left **live** so the
real proposer / auditor / explain prompts run: a prompt-engineering regression (a missed conflict,
a hallucinated deny, a non-verbatim quote) fails a fixture here where a mocked suite could not see
it. The candidate ``policy_text`` is supplied **directly** to ``check_policy_conflicts`` (the
diagnostic seeds it from input -- no policy-source stub needed).

Gating: the module is marked **both** ``integration`` and ``llm``. ``integration`` -> the routine
``-m "not integration"`` run deselects it (its collected count is unchanged). ``llm`` -> it can be
selected on its own (``-m llm``) without a cluster or Keycloak. The autouse
``require_env_or_skip("LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY")`` fixture makes every test **skip
cleanly** (never crash, never false-pass) when the endpoint is unset.

Planting choice (documented): all three fixtures are planted on the **scope-focal** branch of the
survey. The focus service is a **Tool** that owns exactly **one** scope (the focal scope); the
offending pair's role is a **user-held realm role** (a ``candidate_role``, resolved from a stubbed
subject). Focus ``type == Tool`` means the AGENT-only role-focal branch never runs, so the survey
evaluates exactly the one planted scope-focal entity -- minimal surface, least nondeterminism, and
the direct/coarse collision is adjudicated against a real, typed ``(candidate_role, own_scope)``
pair exactly as a scope-focal run does.
"""

from unittest.mock import patch

import pytest

from aiac.agent.policy_rules_builder.diagnostic import _verify_quote
from aiac.agent.policy_rules_builder.diagnostic_models import ConflictStatus
from aiac.idp.configuration.models import Role, Scope, Service, ServiceType, Subject
from aiac.agent.uc.policy_check.check import check_policy_conflicts
from test.integration.launcher import require_env_or_skip

pytestmark = [pytest.mark.integration, pytest.mark.llm]

# The Keycloak internal client UUID the /apply/service/{id} route and check_policy_conflicts key on.
FOCUS_ID = "svc-focus-uuid"


# --------------------------------------------------------------------------- #
# Harness                                                                     #
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _require_llm_env():
    """Skip the whole suite cleanly unless a real LLM endpoint is configured. Autouse so every
    fixture is gated without repeating the call; runs before the patched survey."""
    require_env_or_skip("LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY")


class _FakeConfig:
    """Duck-typed stand-in for ``Configuration`` -- ``check_policy_conflicts`` and
    ``resolve_focal_entities`` only ever call ``get_services()`` / ``get_subjects()`` on it. Patched
    in at ``focal_entities._config`` so the single seam drives both the ``service_type`` lookup and
    the resolver."""

    def __init__(self, services: list[Service], subjects: list[Subject]):
        self._services = services
        self._subjects = subjects

    def get_services(self) -> list[Service]:
        return self._services

    def get_subjects(self) -> list[Subject]:
        return self._subjects


def _scope(id: str, name: str, description: str) -> Scope:
    """An aiac.managed scope owned by the focus Tool -- becomes an ``own_scope`` (a scope-focal run)."""
    return Scope(
        id=id,
        name=name,
        description=description,
        attributes={"aiac.managed": "true"},
        serviceId="focus-tool",
    )


def _role(id: str, name: str, description: str) -> Role:
    """A realm role held by the stubbed user -- becomes a ``candidate_role`` (membership-derived, not
    aiac.managed, and not owned by any service)."""
    return Role(id=id, name=name, description=description, composite=False, childRoles=[])


def _run(policy_text: str, *, focal_scope: Scope, candidate_role: Role):
    """Drive ``check_policy_conflicts`` end-to-end with a stubbed one-scope Tool focus and a single
    user-held candidate role. Only the catalog seam is patched; the LLM is left live."""
    focus = Service(
        id=FOCUS_ID,
        serviceId="focus-tool",
        enabled=True,
        type=ServiceType.TOOL,  # Tool => role-focal (AGENT-only) branch never runs.
        roles=[],
        scopes=[focal_scope],
    )
    subject = Subject(id="u-planted", username="planted-user", enabled=True, roles=[candidate_role])
    with patch(
        "aiac.agent.shared.focal_entities._config",
        return_value=_FakeConfig([focus], [subject]),
    ):
        return check_policy_conflicts(policy_text, FOCUS_ID)


def _pairs(report) -> set[tuple[str, str]]:
    """Confirmed conflict set as ``(role.name, scope.name)`` pairs (matched by name/id-agnostic name)."""
    return {(c.role.name, c.scope.name) for c in report.conflicts}


def _assert_quotes_verbatim(report, policy_text: str) -> None:
    """Every string in every conflict's ``granting_quotes`` / ``prohibiting_quotes`` must be a
    verbatim (whitespace-normalized) substring of ``policy_text`` -- reusing the engine's own
    ``_verify_quote`` so the normalization matches exactly."""
    for conflict in report.conflicts:
        for quote in conflict.granting_quotes + conflict.prohibiting_quotes:
            assert _verify_quote(quote, policy_text), (
                f"quote is not a verbatim substring of policy_text: {quote!r}"
            )


# --------------------------------------------------------------------------- #
# Fixture 1 -- DIRECT conflict (scope-focal). The policy both grants and         #
# prohibits the SAME (developer, source-write) pair on the same action ("write"),#
# a direct contradiction. The confirmed set must contain that pair.             #
# --------------------------------------------------------------------------- #
def test_direct_conflict_contains_planted_pair():
    write = _scope("sc-write", "source-write", "Write and modify source code in the repository.")
    developer = _role("role-dev", "developer", "A software developer.")

    policy = (
        "Developers may write to the source code repository. "
        "Developers must not write to the source code repository."
    )

    report = _run(policy, focal_scope=write, candidate_role=developer)

    assert report.status == ConflictStatus.CONFLICTS_FOUND
    assert ("developer", "source-write") in _pairs(report)
    _assert_quotes_verbatim(report, policy)


# --------------------------------------------------------------------------- #
# Fixture 2 -- COARSE-SCOPE conflict (scope-focal). The focal scope is COARSE     #
# ("manage ... reading and modifying"); the policy grants the read facet and      #
# prohibits the write facet of that single coarse scope -- a granularity mismatch #
# on (developer, issues-manage). The confirmed set must contain that pair.        #
# --------------------------------------------------------------------------- #
def test_coarse_scope_conflict_contains_planted_pair():
    issues = _scope(
        "sc-iss",
        "issues-manage",
        "Manage the issue tracker, including reading and modifying issues.",
    )
    developer = _role("role-dev", "developer", "A software developer.")

    policy = (
        "Developers may read the issue tracker. "
        "Developers must not modify the issue tracker."
    )

    report = _run(policy, focal_scope=issues, candidate_role=developer)

    assert report.status == ConflictStatus.CONFLICTS_FOUND
    assert ("developer", "issues-manage") in _pairs(report)
    _assert_quotes_verbatim(report, policy)


# --------------------------------------------------------------------------- #
# Fixture 3 -- CLEAN policy (scope-focal). A single grant, no prohibition:        #
# exactly one entity evaluated, no conflict, nothing unevaluated => no_conflict.  #
# --------------------------------------------------------------------------- #
def test_clean_policy_is_no_conflict():
    deploy = _scope("sc-dep", "deploy", "Deploy the application to production.")
    operator = _role(
        "role-ops", "operator", "An operations engineer who deploys and runs the application."
    )

    policy = "Operators may deploy the application to production."

    report = _run(policy, focal_scope=deploy, candidate_role=operator)

    assert report.status == ConflictStatus.NO_CONFLICT
    assert report.conflicts == []
