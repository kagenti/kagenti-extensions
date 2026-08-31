"""Scenario 6 — ambiguous clause: 1 user, 1 agent, 1 tool, education/registrar domain.

Companion to ``scenario_eval_baseline.py`` (Scenario 1) for ``test_policy_pipeline_eval.py`` (spec:
``docs/specs/eval/policy-eval-scenarios.md``). Isolates one aspect: a broad-sounding grant clause
whose scope is narrowed by an explicit in-clause qualifier.

The policy text grants "user-role-enrollment-advisor ... access to enrollment information," which
read in isolation could plausibly stretch to cover ``tool-scope-enrollment-history`` too, since a
student's historical record is arguably itself a form of "enrollment information." But the same
clause immediately qualifies itself: "enrollment information" is defined, for advising purposes, as
"a student's current enrollment status only." That qualifier makes the narrow reading
(``tool-scope-enrollment-status``) the only one the text actually supports — ground truth below
encodes exactly that. A real LLM-backed PRB run that also grants ``tool-scope-enrollment-history``
here has missed the qualifier and over-granted: a genuine bug worth investigating, not an excused
alternate reading.

The agent's own role (``agent-role-registrar-operations``) is deliberately granted BOTH scopes (it is capable
of reaching either), so the ambiguity lives entirely on the subject side of the per-scope AND —
this scenario tests whether the PRB resolves the ambiguous user-facing clause narrowly, not whether
the agent-facing capability gate is populated correctly (that is Scenario 1's job).

Pure data: no imports beyond ``__future__``, mirroring ``scenario_eval_baseline.py``.
"""

from __future__ import annotations

# --- Realm ------------------------------------------------------------------------------------

REALM_DEFAULT = "aiac-pp-eval-ambiguous-clause"
POLICY_FILE = "policy.eval_ambiguous_clause.md"

# --- Agents -------------------------------------------------------------------------------------

AGENTS: dict[str, dict] = {
    "team1/registrar-agent": {
        "description": (
            "Autonomous Agent acting on a user's behalf against the student enrollment system. "
            "It reads a student's current enrollment status and historical enrollment record."
        ),
        "inbound_scopes": {
            "agent-scope-enrollment-status-access": (
                "Scope granting use of the registrar agent's current-enrollment-status lookup "
                "capability."
            ),
            "agent-scope-enrollment-history-access": (
                "Scope granting use of the registrar agent's enrollment-history lookup capability."
            ),
        },
        "delegation_scopes": {},
        "roles": {
            "agent-role-registrar-operations": (
                "Covers reading a student's current enrollment status and historical enrollment "
                "record."
            ),
        },
    },
}

# --- Tools --------------------------------------------------------------------------------------

TOOLS: dict[str, dict] = {
    "enrollment-tool": {
        "description": (
            "Capability provider Tool for student enrollment records. It performs read "
            "operations on a student's current status and historical enrollment record."
        ),
        "scopes": {
            "tool-scope-enrollment-status": (
                "Read a student's current enrollment status (enrolled, withdrawn, or on leave). "
                "Read-only."
            ),
            "tool-scope-enrollment-history": (
                "Read a student's historical enrollment record across terms, including past "
                "status changes. Read-only."
            ),
        },
    },
}

# --- Users ----------------------------------------------------------------------------------

USERS: dict[str, str] = {
    "advisor-user": "user-role-enrollment-advisor",
}

USER_PASSWORD = "password"

USER_ROLES: dict[str, str] = {
    "user-role-enrollment-advisor": "Enrollment Advisor — authorized to access enrollment information for advising purposes.",
}

# --- Role -> access facts (name-level; the single source of truth) --------------------------

# Split into two narrow scopes (rather than one scope whose description bundles both
# capabilities), and grant the advisor only the status half. Granting both would restate, in
# the inbound clause itself, that the advisor has a history capability — an even more direct
# textual assertion than the old bundled description, and still a contradiction against the
# outbound qualifier below that withholds history access. Nothing in this scenario grants
# agent-scope-enrollment-history-access to anyone, matching the outbound-subject side exactly.
INBOUND_PAIRS: list[tuple[str, str]] = [
    ("user-role-enrollment-advisor", "agent-scope-enrollment-status-access"),
]

OUTBOUND_PAIRS: list[tuple[str, str]] = [
    ("agent-role-registrar-operations", "tool-scope-enrollment-status"),
    ("agent-role-registrar-operations", "tool-scope-enrollment-history"),
]

# The leading phrase "access to enrollment information" reads broadly in isolation and could
# plausibly cover tool-scope-enrollment-history too, but the same clause's qualifier ("current
# enrollment status only") makes the narrow reading (tool-scope-enrollment-status) the only one the
# text actually supports. A real LLM-backed PRB run that also grants tool-scope-enrollment-history
# here has missed the qualifier — a genuine over-grant to flag, not an excused alternate reading.
OUTBOUND_SUBJECT_PAIRS: list[tuple[str, str]] = [
    ("user-role-enrollment-advisor", "tool-scope-enrollment-status"),
]
