"""Scenario 6 — ambiguous clause: 1 user, 1 agent, 1 tool, education/registrar domain.

Companion to ``scenario_eval_baseline.py`` (Scenario 1) for ``test_policy_pipeline_eval.py`` (spec:
``docs/specs/integration-test/policy-eval-scenarios.md``). Isolates one aspect: a single genuinely
multi-interpretable (non-contradictory) clause.

The policy text grants "enrollment-advisor ... access to enrollment information." A reasonable
reader could take this narrowly (the ``enrollment-status`` scope only) or broadly (also
``enrollment-history``, since a student's historical record is arguably itself a form of
"enrollment information"). Neither reading contradicts the other. Ground truth below encodes ONLY
the narrower reading (``enrollment-status``) per this suite's most-restrictive-reading-wins rule.
THIS IS A DELIBERATE RISK, NOT A BUG: the real LLM-backed PRB may reasonably converge on the
broader reading instead, and a mismatch on this specific pair is a legitimate finding for this
evaluation suite to surface — it is not evidence the scenario itself is authored wrong.

The agent's own role (``registrar_operations``) is deliberately granted BOTH scopes (it is capable
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
    "registrar-agent": {
        "description": (
            "Autonomous Agent acting on a user's behalf against the student enrollment system. "
            "It reads a student's current enrollment status and historical enrollment record."
        ),
        "inbound_scopes": {
            "registrar-access": (
                "Scope granting use of the registrar agent's enrollment-information capability — "
                "reading enrollment status and enrollment history."
            ),
        },
        "target_scopes": {},
        "roles": {
            "registrar_operations": (
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
            "enrollment-status": (
                "Read a student's current enrollment status (enrolled, withdrawn, or on leave). "
                "Read-only."
            ),
            "enrollment-history": (
                "Read a student's historical enrollment record across terms, including past "
                "status changes. Read-only."
            ),
        },
    },
}

# --- Users ----------------------------------------------------------------------------------

USERS: dict[str, str] = {
    "advisor-user": "enrollment-advisor",
}

USER_PASSWORD = "password"

USER_ROLES: dict[str, str] = {
    "enrollment-advisor": "Enrollment Advisor — authorized to access enrollment information for advising purposes.",
}

# --- Role -> access facts (name-level; the single source of truth) --------------------------

INBOUND_PAIRS: list[tuple[str, str]] = [
    ("enrollment-advisor", "registrar-access"),
]

OUTBOUND_PAIRS: list[tuple[str, str]] = [
    ("registrar_operations", "enrollment-status"),
    ("registrar_operations", "enrollment-history"),
]

# Ambiguous clause: "access to enrollment information" could plausibly cover enrollment-history
# too (rollback-equivalent: historical record is arguably itself a form of enrollment
# information). Ground truth encodes ONLY the narrower reading (enrollment-status) per this
# suite's most-restrictive-reading-wins rule. A real LLM-backed PRB run that also grants
# enrollment-history here is a legitimate finding for this evaluation suite, not evidence this
# scenario is authored incorrectly.
OUTBOUND_SUBJECT_PAIRS: list[tuple[str, str]] = [
    ("enrollment-advisor", "enrollment-status"),
]
