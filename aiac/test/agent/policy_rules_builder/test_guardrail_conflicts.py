"""Documents a guardrail gap: the Policy Rules Builder (PRB) has no whole-document pre-flight
validator anywhere in this codebase. ``build_role_rules``/``build_scope_rules`` each only ever see
one role-vs-many-scopes (or scope-vs-many-roles) mapping call at a time; nothing inspects the
policy document as a whole, across all its clauses, before any mapping call runs.

This test exercises a policy document that contains a direct, unresolvable logical contradiction
about a single (role, scope) pair: one clause grants ``release-user`` the ``deploy-trigger``
operation, and a later clause states that ``release-user`` must never be granted
``deploy-trigger`` under any circumstance. There is no way to "read the clauses together" and
arrive at a single consistent answer -- the document asserts both a grant and a revocation of the
exact same edge.

The intended contract (once a whole-document guardrail exists) is deny-wins-on-conflict: a
document containing this kind of direct contradiction should be rejected outright, before any
per-mapping propose/precheck/audit cycle runs, by raising ``PolicyRulesBuilderError``. Today,
absent that guardrail, the call instead proceeds into the normal per-cell propose/audit flow and
the LLM silently resolves the contradiction one way or the other -- it does not raise. This test is
therefore expected to fail (XFAIL) against current behavior; it exists to pin the intended contract
so that a future whole-document guardrail has a regression test waiting for it. See
docs/specs/eval/policy-eval-scenarios.md for the broader guardrail-scenario catalog
this test belongs to.

Requires a live LLM (``@pytest.mark.integration``); skips when ``LLM_BASE_URL`` is unset. It does
not touch Keycloak or any service -- it calls ``build_scope_rules`` directly with a temp policy
file, matching the pattern in ``test_auditor_dimension_integration.py``.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from aiac.idp.configuration.models import Role, Scope

pytestmark = pytest.mark.integration

# release-user is both granted and (later, explicitly) denied deploy-trigger -- a direct
# contradiction on the same (role, scope) pair, not a merely ambiguous or multi-interpretable
# document.
_POLICY = """\
# Access Control Policy

Grant access on a least-privilege basis; deny by default.

## Users -> tool operations (subject may reach the tool)
- release-user may perform deploy-trigger and deploy-status.
- qa-user may perform deploy-status.

## Restrictions on deployment operations
- release-user must never be granted deploy-trigger under any circumstance; this permission is
  permanently revoked pending the outcome of the ongoing security review and must not be
  reinstated by any other clause in this document.
"""

_USER_ROLES = {
    "release-user": "Release manager who prepares and ships production releases.",
    "qa-user": "Quality-assurance engineer who verifies release readiness before rollout.",
}
_DEPLOY_TRIGGER_DESC = "Trigger a production deployment for a release."


@pytest.fixture
def _conflicting_policy():
    """Point AIAC_POLICY_FILE at the contradictory policy; skip when no live LLM is configured."""
    if not os.getenv("LLM_BASE_URL"):
        pytest.skip("LLM_BASE_URL unset — PRB auditor regression needs a live LLM")
    f = tempfile.NamedTemporaryFile("w", suffix=".md", delete=False)
    f.write(_POLICY)
    f.close()
    prev = os.environ.get("AIAC_POLICY_FILE")
    os.environ["AIAC_POLICY_FILE"] = f.name
    try:
        yield
    finally:
        Path(f.name).unlink(missing_ok=True)
        if prev is None:
            os.environ.pop("AIAC_POLICY_FILE", None)
        else:
            os.environ["AIAC_POLICY_FILE"] = prev


@pytest.mark.xfail(
    strict=True,
    reason="no whole-document guardrail exists yet — see docs/specs/eval/policy-eval-scenarios.md",
)
def test_guardrail_rejects_direct_grant_revoke_contradiction(_conflicting_policy):
    """A document that both grants and (elsewhere) permanently revokes the same (release-user,
    deploy-trigger) pair should be rejected outright -- before any mapping call produces rules --
    once a whole-document guardrail exists. Today there is no such guardrail: the PRB has no
    pre-flight step that inspects the document as a whole, so this call proceeds into the normal
    propose/precheck/audit cycle and the LLM resolves the contradiction per-cell instead of
    refusing the document. ``strict=True`` means an unexpected pass (the call coincidentally
    raising today) is reported as a hard failure rather than silently ignored, so a real guardrail
    landing is what turns this XFAIL into a genuine pass instead of a silent no-op.
    """
    from aiac.agent.policy_rules_builder.graph import PolicyRulesBuilderError, build_scope_rules

    user_roles = [
        Role(id=f"role-{name}", name=name, description=desc, composite=False)
        for name, desc in _USER_ROLES.items()
    ]
    deploy_trigger = Scope(id="scope-deploy-trigger", name="deploy-trigger", description=_DEPLOY_TRIGGER_DESC)

    with pytest.raises(PolicyRulesBuilderError):
        build_scope_rules(user_roles, deploy_trigger)
