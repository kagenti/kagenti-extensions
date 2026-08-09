"""Scenario 3 — missing-details: 5 users, 3 agents, 4 tools, with silent authoring gaps.

Companion to ``scenario_eval_baseline.py`` (Scenario 1) for ``test_policy_pipeline_eval.py``
(spec: ``docs/specs/integration-test/policy-eval-scenarios.md``). Unlike Scenario 1 (clean and
unambiguous) and Scenario 2 (contradictions), this scenario's character is *silent gaps*: ordinary
policy-authoring oversights that, once run through deny-by-default access control, produce
emergent (not hand-picked) unreachable agents/tools and zero-access users — nobody wrote "deny
this", the denial just falls out of what the policy document never got around to saying. It also
deliberately includes one genuinely multi-interpretable (non-contradictory) clause and two
wildcard-phrased grants, so both reach the real PRB/PCE instead of being deflected by a guardrail.

Five things this scenario demonstrates, all of which fall out of ``policy.eval_unreachable.md``
plus the tables below (see inline comments at each site for the mechanism):

1. **``archive-agent`` is a fully unreachable agent** (see ``EXPECT_NO_REGO``). It has a real
   Keycloak client, an inbound scope, and a client role — it was provisioned like any other
   agent — but the policy document's "Users -> agent capabilities" and "Agent roles -> tool
   operations" sections simply never mention it. A plausible real-world cause: the archive
   service was stood up ahead of the access policy that was meant to cover it, and the policy
   author never circled back.
2. **``credentials-tool`` is an unreachable tool.** It exists with a real scope
   (``credentials-read``) but no agent role is ever granted it anywhere in the policy text — an
   omission in the "Agent roles -> tool operations" section, not a deliberate denial.
3. **``auditor-user`` is a zero-access user.** Its role, ``compliance-auditor``, was created (a
   realm role exists, the user holds it) for a compliance-reporting capability that hasn't been
   policy-described yet, so it appears in neither ``INBOUND_PAIRS`` nor
   ``OUTBOUND_SUBJECT_PAIRS`` — deny-by-default leaves the user with no access anywhere.
4. **Genuinely multi-interpretable phrasing.** The policy text grants
   "release-coordinator ... access to deployment status information". A reasonable reader could
   take this narrowly (the ``deploy-status`` scope only) or broadly (also ``deploy-rollback``,
   since rollback history is arguably itself a form of deployment status). Neither reading
   contradicts the other. Ground truth below encodes ONLY the narrower reading (``deploy-status``)
   per this suite's most-restrictive-reading-wins rule. THIS IS A DELIBERATE RISK, NOT A BUG: the
   real LLM-backed PRB may reasonably converge on the broader reading instead, and a mismatch on
   this specific pair is a legitimate finding for this evaluation suite to surface — it is not
   evidence the scenario itself is authored wrong.
5. **Wildcard-phrased grants.** Both the user-facing and agent-facing sections grant
   "all deployment operations" (rather than enumerating scope names) for the release capability.
   Ground truth enumerates the intended concrete expansion — ``deploy-trigger``, ``deploy-status``,
   ``deploy-rollback`` (3 scopes) — so the test can check whether the real PRB expands the wildcard
   correctly instead of under- or over-granting.

Pure data: no imports beyond ``__future__``, mirroring ``scenario_eval_baseline.py``.
"""

from __future__ import annotations

# --- Realm ------------------------------------------------------------------------------------

REALM_DEFAULT = "aiac-pp-eval-unreachable"
POLICY_FILE = "policy.eval_unreachable.md"

# --- Agents -------------------------------------------------------------------------------------
#
# Same shape as scenario_eval_baseline.py. No agent here has a ``target_scopes`` entry — this
# scenario does not use the agent-to-agent delegation mechanism (that is Scenario 1's device);
# every agent's ``target_scopes`` is left as ``{}``.

AGENTS: dict[str, dict] = {
    "service-desk-agent": {
        "description": (
            "Autonomous Agent acting on a user's behalf against an internal ticketing system. It "
            "reads and updates support tickets on the requester's behalf."
        ),
        "inbound_scopes": {
            "desk-access": (
                "Scope granting use of the service-desk agent's ticketing capability — reading "
                "and updating support tickets."
            ),
        },
        "target_scopes": {},
        "roles": {
            "desk_operations": (
                "Covers read and write access to the ticketing system — reading and updating "
                "support tickets."
            ),
        },
    },
    "release-agent": {
        "description": (
            "Autonomous Agent acting on a user's behalf to coordinate software releases. It "
            "triggers deployments, reports and rolls back deployment status, and keeps the "
            "release runbook (an internal wiki) up to date."
        ),
        "inbound_scopes": {
            "release-access": (
                "Scope granting use of the release agent's deployment-coordination capability — "
                "triggering, monitoring, and rolling back deployments, and updating the release "
                "runbook."
            ),
        },
        "target_scopes": {},
        "roles": {
            # Wildcard grant #1 (agent-facing half): the policy text says "all deployment
            # operations" rather than enumerating scopes. Ground truth expands this to all three
            # concrete deploy-tool scopes below (item 5 in the module docstring).
            "release_operations": (
                "Covers all deployment operations against the deployment tool — triggering a "
                "deployment, reading its status, and rolling it back."
            ),
            "content_operations": (
                "Covers read and write access to the release runbook wiki — reading and editing "
                "runbook pages."
            ),
        },
    },
    "archive-agent": {
        "description": (
            "Autonomous Agent intended to manage long-term archival of completed release "
            "records. Provisioned ahead of the access policy meant to govern it; no policy "
            "language yet describes who may call it or what it may reach."
        ),
        "inbound_scopes": {
            "archive-access": (
                "Scope granting use of the archive agent's records-retention capability — "
                "retrieving and archiving completed release records. Not yet granted to any user "
                "role in the policy document."
            ),
        },
        "target_scopes": {},
        "roles": {
            "archive_operations": (
                "Covers read and write access to the release-records archive. Not yet granted to "
                "any target in the policy document."
            ),
        },
    },
}

# --- Tools --------------------------------------------------------------------------------------

TOOLS: dict[str, dict] = {
    "ticket-tool": {
        "description": (
            "Capability provider Tool for an internal ticketing system. It performs read and "
            "write operations on support tickets."
        ),
        "scopes": {
            "ticket-read": "Read support tickets and their history. Read-only.",
            "ticket-write": "Create and update support tickets: open, edit, and close.",
        },
    },
    "deploy-tool": {
        "description": (
            "Capability provider Tool for deployment orchestration. It triggers deployments, "
            "reports their status, and rolls them back."
        ),
        "scopes": {
            "deploy-trigger": "Trigger a deployment of a service to its runtime environment.",
            "deploy-status": "Read the current status of a deployment. Read-only.",
            "deploy-rollback": "Roll back a deployment to its previous known-good state.",
        },
    },
    "wiki-tool": {
        "description": (
            "Capability provider Tool for an internal release runbook wiki. It performs read and "
            "write operations on runbook pages."
        ),
        "scopes": {
            "wiki-read": "Read release runbook pages. Read-only.",
            "wiki-write": "Create and edit release runbook pages.",
        },
    },
    "credentials-tool": {
        "description": (
            "Capability provider Tool for stored release credentials. It performs read "
            "operations on credentials used during deployment execution. No agent role is ever "
            "granted its scope anywhere in the policy document — it is unreachable by design."
        ),
        "scopes": {
            "credentials-read": "Read stored release credentials. Read-only.",
        },
    },
}

# --- Users ----------------------------------------------------------------------------------

USERS: dict[str, str] = {
    "desk-user": "support-rep",
    "release-user": "release-engineer",
    "coordinator-user": "release-coordinator",
    "wiki-user": "content-editor",
    # Zero-access user (item 3 in the module docstring): compliance-auditor is a real realm role
    # held by a real user, created ahead of a compliance-reporting capability that the policy
    # document does not describe yet. It appears in neither INBOUND_PAIRS nor
    # OUTBOUND_SUBJECT_PAIRS below, so deny-by-default leaves this user with no access anywhere.
    "auditor-user": "compliance-auditor",
}

USER_PASSWORD = "password"

# name -> description. Realm roles held by users.
USER_ROLES: dict[str, str] = {
    "support-rep": (
        "Support Representative — authorized to read and update support tickets on behalf of "
        "requesters; not involved in releases or the runbook."
    ),
    "release-engineer": (
        "Release Engineer — authorized to trigger deployments and read and roll back their "
        "status as part of day-to-day release execution."
    ),
    "release-coordinator": (
        "Release Coordinator — authorized to access deployment status information for release "
        "oversight purposes; does not trigger or roll back deployments directly."
    ),
    "content-editor": (
        "Content Editor — authorized to read and edit the release runbook wiki; not involved in "
        "triggering or monitoring deployments."
    ),
    "compliance-auditor": (
        "Compliance Auditor — a role created for an upcoming compliance-reporting capability; no "
        "access grants have been written for it yet."
    ),
}

# --- Role -> access facts (name-level; the single source of truth) --------------------------
#
# Same three pair-sets as scenario_eval_baseline.py. Scope names are unique across the whole
# scenario, so (role, scope) pairs are unambiguous; ownership is resolved by scanning
# AGENTS/TOOLS, not encoded here.

INBOUND_PAIRS: list[tuple[str, str]] = [
    ("support-rep", "desk-access"),
    ("release-engineer", "release-access"),
    ("release-coordinator", "release-access"),
    ("content-editor", "release-access"),
    # No row for compliance-auditor (zero-access user, item 3) and no row naming archive-access
    # (archive-agent is fully unreachable, item 1) — both silent gaps by design.
]

# Agent role -> target scope.
OUTBOUND_PAIRS: list[tuple[str, str]] = [
    ("desk_operations", "ticket-read"),
    ("desk_operations", "ticket-write"),
    # Wildcard grant #1 expansion (item 5): "release_operations may perform all deployment
    # operations" -> all three concrete deploy-tool scopes.
    ("release_operations", "deploy-trigger"),
    ("release_operations", "deploy-status"),
    ("release_operations", "deploy-rollback"),
    ("content_operations", "wiki-read"),
    ("content_operations", "wiki-write"),
    # No row names archive_operations (archive-agent is fully unreachable, item 1) and no row
    # names credentials-read (credentials-tool is unreachable, item 2) — both silent gaps by
    # design.
]

OUTBOUND_SUBJECT_PAIRS: list[tuple[str, str]] = [
    ("support-rep", "ticket-read"),
    ("support-rep", "ticket-write"),
    # Wildcard grant #1 expansion (item 5), user-facing half.
    ("release-engineer", "deploy-trigger"),
    ("release-engineer", "deploy-status"),
    ("release-engineer", "deploy-rollback"),
    # Ambiguous clause (item 4): "release-coordinator ... access to deployment status
    # information" could plausibly be read to also cover deploy-rollback (rollback history is
    # arguably itself a form of deployment status). Ground truth encodes ONLY the narrower
    # reading (deploy-status) per this suite's most-restrictive-reading-wins rule. A real
    # LLM-backed PRB run that instead grants deploy-rollback here is a legitimate finding for
    # this evaluation suite, not evidence this scenario is authored incorrectly.
    ("release-coordinator", "deploy-status"),
    ("content-editor", "wiki-read"),
    ("content-editor", "wiki-write"),
    # No row for compliance-auditor (zero-access user, item 3).
]

# --- Emergent unreachability -----------------------------------------------------------------
#
# archive-agent: zero rows above name archive-access (its only inbound scope) or
# archive_operations (its only role), and no other agent has any target_scopes for it to be
# granted through (every agent's target_scopes is {} in this scenario) — truly unreachable from
# every direction in this scenario's own ground truth, not merely "forgotten to grant".
EXPECT_NO_REGO: frozenset[str] = frozenset({"archive-agent"})
