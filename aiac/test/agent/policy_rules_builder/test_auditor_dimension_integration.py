"""Integration regression for the relationship-scoping mapping rule (prompts._MAPPING_RULES, rule 4).

Reproduces a field failure: a policy that grants a tool operation to exactly one subject AND names
that same operation under a *different* relationship (agent-role -> tool operations) made the LLM
auditor conflate the two relationships and wrongly deny the subject's grant. The trigger was the
singular actor-noun agent role name ``issue-operator``, which the auditor read as competing with the
``tester`` subject; it rejected the valid ``(tester, issues-write)`` grant, aborting the whole PRB run.

The fix is a relationship-scoping meta-rule: a policy statement about an entity that is NOT among the
candidates describes a different relationship and is not evidence for or against a candidate's grant.
Originally auditor-only; now shared by the proposer and auditor (``_MAPPING_RULES``) so the two sides
decide grants under the same reasoning. This test pins the known-bad singular name so the collision
cannot silently return.

Requires a live LLM (``@pytest.mark.integration``); skips when ``LLM_BASE_URL`` is unset. It does not
touch Keycloak or any service — it calls ``build_scope_rules`` directly with a temp policy file.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from aiac.idp.configuration.models import Role, Scope

pytestmark = pytest.mark.integration

# Section 3 deliberately uses the SINGULAR ``issue-operator`` that historically tripped the auditor.
# ``tester`` is granted ``issues-write`` ONLY under "Users -> tool operations"; ``issue-operator`` is a
# different (agent-role) relationship and must not count against tester's grant.
_POLICY = """\
# Access Control Policy

Grant access on a least-privilege basis; deny by default.

## Users -> tool operations (subject may reach the tool)
- developer may perform source-read, source-write, and issues-read.
- tester may perform issues-read and issues-write.

## Agent roles -> tool operations (agent may reach the tool)
- source-operator may perform source-read and source-write.
- issue-operator may perform issues-read and issues-write.
"""

_USER_ROLES = {
    "developer": "Developer — an engineering user who writes and maintains source code.",
    "tester": "Tester — a quality-assurance user who files, triages, and updates issue reports.",
    "devops": "DevOps — an operations user who manages deployment infrastructure and runtime environments.",
}
_ISSUES_WRITE_DESC = "Create and update issues: open, edit, comment, and close."


@pytest.fixture
def _bad_name_policy():
    """Point AIAC_POLICY_FILE at the known-bad policy; skip when no live LLM is configured."""
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


def test_auditor_admits_single_subject_grant_despite_competing_relation(_bad_name_policy):
    """(tester, issues-write) must be granted even though the singular ``issue-operator`` agent role
    names issues-write under a different relationship. Pre-fix, the auditor rejected and the builder
    raised PolicyRulesBuilderError; post-fix it returns exactly the tester grant."""
    from aiac.agent.policy_rules_builder.graph import build_scope_rules

    user_roles = [
        Role(id=f"role-{name}", name=name, description=desc, composite=False) for name, desc in _USER_ROLES.items()
    ]
    issues_write = Scope(id="scope-issues-write", name="issues-write", description=_ISSUES_WRITE_DESC)

    rules = build_scope_rules(user_roles, issues_write)

    granted = {r.role.name for r in rules}
    assert granted == {"tester"}, f"expected only tester granted issues-write, got {sorted(granted)}"
