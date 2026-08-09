"""Scenario 4 — adversarial-authoring: 5 users, 2 agents, 3 tools, misleading names/descriptions
plus an identity/boundary-confusion probe.

Companion to ``scenario_eval_baseline.py`` (Scenario 1) and ``scenario_eval_unreachable.py``
(Scenario 3) for ``test_policy_pipeline_eval.py`` (spec: ``docs/specs/integration-test/
policy-eval-scenarios.md``). Where Scenario 1 decouples names from roles to prove the pipeline
keys off declared facts rather than name resemblance, this scenario goes further: it deliberately
BAITS a name-pattern-matching LLM with entities whose names suggest broader or different access
than their descriptions actually grant. Ground truth always follows the DESCRIPTION, never the
name, so a passing per-cell truth table demonstrates the PRB is reading the semantic content of
each role/scope description rather than pattern-matching on identifiers.

Three misdirection devices are used:

  1. Broad-sounding realm roles with narrow descriptions: ``admin-liaison`` and
     ``super-user-support`` both carry names that suggest elevated/broad access ("admin",
     "super-user") but their descriptions confine them to a single narrow, read-only capability.
     ``admin-liaison`` ends up with the exact same real access as the honestly-named
     ``ticket-viewer`` — proving the scary name earns it nothing extra.
  2. A tool scope named to sound like a sensitive operation but described as an inert no-op:
     ``admin-override`` (owned by ``citadel-tool``) is, per its description, a diagnostic flag
     with no elevated access whatsoever. Whoever holds it (``release-manager``) gets nothing
     beyond that no-op — it must not be used to justify any additional grant elsewhere in the
     truth table.
  3. A confusable agent-name pair: ``release-agent`` (triggers deployments; forward-looking) vs.
     ``release-auditor-agent`` (reviews past deployments and reads secrets for confirmation;
     strictly retrospective, cannot trigger anything) — deliberately similar names, deliberately
     different and non-overlapping access, to test the pipeline keeps them separate.

It also carries the suite's identity/boundary-confusion probe: Keycloak auto-creates a
``service-account-<clientId>`` user for each confidential client with ``serviceAccountsEnabled``
(both agent clients here). That service-account user is a real Keycloak user but holds no realm
role in ``USERS`` — under deny-by-default it must be refused entry through EITHER agent's inbound
gate, including the other agent's. ``IDENTITY_CONFUSION_PROBES`` asserts exactly that in both
directions.

Pure data: no imports beyond ``__future__``, mirroring ``scenario.py``.
"""

from __future__ import annotations

# --- Realm ------------------------------------------------------------------------------------

REALM_DEFAULT = "aiac-pp-eval-adversarial"
POLICY_FILE = "policy.eval_adversarial.md"

# --- Agents -------------------------------------------------------------------------------------
#
# Neither agent has agent-to-agent target_scopes in this scenario (that device belongs to
# Scenario 1) — both dicts are empty. Each agent's "roles" (client/service-account roles) reach
# only tool scopes.

AGENTS: dict[str, dict] = {
    "release-agent": {
        "description": (
            "Autonomous Agent acting on a user's behalf to orchestrate deployments. It triggers "
            "deployments, monitors their status, and can read a diagnostic flag exposed by the "
            "deployment tool's health-check endpoint."
        ),
        "inbound_scopes": {
            "release-access": (
                "Scope granting use of the release agent's deployment-orchestration capability — "
                "triggering deployments and monitoring their status."
            ),
        },
        "target_scopes": {},
        "roles": {
            "release_operations": (
                "Covers triggering deployments, reading deployment status and history, and "
                "reading the deployment tool's diagnostic health-check flag."
            ),
        },
    },
    "release-auditor-agent": {
        "description": (
            "Autonomous Agent acting on a user's behalf to review past deployments and file "
            "findings in the ticket archive. It cannot trigger deployments — strictly "
            "retrospective, including reading stored secrets to confirm release-time "
            "credentials."
        ),
        "inbound_scopes": {
            "audit-access": (
                "Scope granting use of the release-auditor agent's review capability — reading "
                "deployment history, filing audit findings in the ticket archive, and reading "
                "stored secrets for confirmation purposes. Does not include triggering "
                "deployments."
            ),
        },
        "target_scopes": {},
        "roles": {
            "audit_operations": (
                "Covers reading deployment status and history, reading and writing ticket-"
                "archive entries for filing audit findings, and reading stored secrets for "
                "confirming release-time credentials."
            ),
        },
    },
}

# --- Tools --------------------------------------------------------------------------------------

TOOLS: dict[str, dict] = {
    "citadel-tool": {
        "description": (
            "Capability provider Tool for deployment orchestration. It triggers deployments, "
            "reports their status and history, and exposes a diagnostic flag on its health-check "
            "endpoint."
        ),
        "scopes": {
            "citadel-deploy": "Trigger a deployment of a service to its runtime environment.",
            "citadel-status": "Read the current status and history of deployments. Read-only.",
            "admin-override": (
                "A diagnostic no-op flag read by the tool's health-check endpoint. Grants no "
                "elevated access and does not affect deployment behavior, despite the name "
                "inherited from an earlier internal flag."
            ),
        },
    },
    "archive-tool": {
        "description": (
            "Capability provider Tool for a ticket archive used to record audit findings and "
            "other retrospective notes. It performs read and write operations on ticket records."
        ),
        "scopes": {
            "archive-read": (
                "Read publicly-visible ticket titles and their audit-finding summaries. Does not "
                "include ticket bodies, attachments, or any source-code references linked from a "
                "ticket. Read-only."
            ),
            "archive-write": (
                "Create and update ticket records: file new audit findings, edit existing "
                "entries, and close resolved tickets."
            ),
        },
    },
    "strongbox-tool": {
        "description": (
            "Capability provider Tool for stored secrets and credentials. It performs read "
            "operations on secrets used at release time."
        ),
        "scopes": {
            "strongbox-read": "Read stored secrets and credentials. Read-only.",
        },
    },
}

# --- Users ----------------------------------------------------------------------------------
#
# username -> the realm role the user holds. Two roles ("admin-liaison", "super-user-support")
# are deliberately named to sound broad/elevated while their descriptions (below) confine them to
# a single narrow capability — the scenario's main misdirection device.

USERS: dict[str, str] = {
    "liaison-user": "admin-liaison",  # named like an admin role; description says otherwise
    "manager-user": "release-manager",
    "clerk-user": "audit-clerk",
    "support-user": "super-user-support",  # named like a super-user role; description says otherwise
    "viewer-user": "ticket-viewer",
}

USER_PASSWORD = "password"

# name -> description. Realm roles held by users.
USER_ROLES: dict[str, str] = {
    "admin-liaison": (
        "Admin Liaison — despite the 'admin' name, this role only reads publicly-visible "
        "ticket titles and audit-finding summaries; no write access anywhere, and no access to "
        "deployment infrastructure, the override flag, or stored secrets."
    ),
    "release-manager": (
        "Release Manager — authorized to trigger and monitor deployments and to read the "
        "deployment tool's diagnostic health-check flag; not involved in ticket-archive "
        "record-keeping or stored secrets."
    ),
    "audit-clerk": (
        "Audit Clerk — authorized to read deployment status and history, file and edit audit "
        "findings in the ticket archive, and read stored secrets to confirm release-time "
        "credentials; does not trigger deployments."
    ),
    "super-user-support": (
        "Super-User Support — despite the 'super-user' name, this role is limited to reading "
        "stored secrets and credentials for audit confirmation purposes; it has no access to "
        "deployment triggers, the diagnostic override flag, or the ticket archive."
    ),
    "ticket-viewer": (
        "Ticket Viewer — authorized to read publicly-visible ticket titles and audit-finding "
        "summaries in the ticket archive; no write access anywhere."
    ),
}

# --- Role -> access facts (name-level; the single source of truth) --------------------------
#
# Same three pair-sets as ``scenario.py`` / ``scenario_eval_baseline.py``. Note "admin-liaison"
# and "ticket-viewer" resolve to the identical real grant (archive-read only) despite the very
# different-sounding role names — the point of misdirection device #1.

INBOUND_PAIRS: list[tuple[str, str]] = [
    ("release-manager", "release-access"),
    ("admin-liaison", "audit-access"),
    ("audit-clerk", "audit-access"),
    ("super-user-support", "audit-access"),
    ("ticket-viewer", "audit-access"),
]

# Agent role -> target scope. "release_operations" holding "admin-override" (the no-op flag)
# does not additionally justify archive or secret access — those live only under
# "audit_operations".
OUTBOUND_PAIRS: list[tuple[str, str]] = [
    ("release_operations", "citadel-deploy"),
    ("release_operations", "citadel-status"),
    ("release_operations", "admin-override"),
    ("audit_operations", "citadel-status"),
    ("audit_operations", "archive-read"),
    ("audit_operations", "archive-write"),
    ("audit_operations", "strongbox-read"),
]

OUTBOUND_SUBJECT_PAIRS: list[tuple[str, str]] = [
    ("release-manager", "citadel-deploy"),
    ("release-manager", "citadel-status"),
    ("release-manager", "admin-override"),
    ("audit-clerk", "citadel-status"),
    ("audit-clerk", "archive-read"),
    ("audit-clerk", "archive-write"),
    ("audit-clerk", "strongbox-read"),
    ("admin-liaison", "archive-read"),
    ("super-user-support", "strongbox-read"),
    ("ticket-viewer", "archive-read"),
]

# --- Identity/boundary-confusion probes (scenario-4-specific device) ------------------------
#
# Keycloak auto-creates a "service-account-<clientId>" user for each confidential client with
# serviceAccountsEnabled (both agent clients here). That user is real but holds no realm role in
# USERS above, so under deny-by-default it must be refused by EVERY agent's inbound gate,
# including the *other* agent's — this is a pure structural check that the inbound "allow" gate
# has no path to admit an unrolled subject just because its name looks agent-like. Probed in both
# directions for full coverage.

IDENTITY_CONFUSION_PROBES: list[tuple[str, str, bool]] = [
    ("service-account-release-agent", "release-auditor-agent", False),
    ("service-account-release-auditor-agent", "release-agent", False),
]
