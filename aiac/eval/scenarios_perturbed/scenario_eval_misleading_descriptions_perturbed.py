"""Semantic-perturbation sibling of ``scenario_eval_misleading_descriptions.py`` (spec:
``docs/specs/eval/policy-eval-robustness-consistency.md``).

Same structure (names, ``USERS``, all pair-lists are byte-identical to the original), reworded
prose only. See ``scenario_eval_baseline_perturbed.py`` for the general rationale. The reworded
descriptions/policy text preserve both misdirection devices from the original: ``user-role-vip-manager``'s
name-vs-description mismatch and ``tool-scope-master-override``'s inert-but-scary-named scope.

Pure data: no imports beyond ``__future__``, mirroring the original.
"""

from __future__ import annotations

# --- Realm ------------------------------------------------------------------------------------

REALM_DEFAULT = "aiac-pp-eval-misleading-descriptions"
POLICY_FILE = "policy.eval_misleading_descriptions_perturbed.md"

# --- Agents -------------------------------------------------------------------------------------

AGENTS: dict[str, dict] = {
    "guest-services-agent": {
        "description": (
            "An autonomous agent that acts for a user against the hotel's guest-services system: "
            "looking up reservation details and guest notes, plus exposing a no-op hook kept "
            "around for internal testing."
        ),
        "inbound_scopes": {
            "agent-scope-guest-access": (
                "Lets a holder use the guest-services agent's reservation and guest-notes lookup "
                "abilities."
            ),
        },
        "delegation_scopes": {},
        "roles": {
            "agent-role-guest-operations": (
                "Covers looking up reservation details and guest notes, plus calling the "
                "diagnostic no-op hook. That hook does nothing and grants nothing beyond itself."
            ),
        },
    },
}

# --- Tools --------------------------------------------------------------------------------------

TOOLS: dict[str, dict] = {
    "reservation-tool": {
        "description": (
            "A capability provider for hotel reservations and guest notes, performing lookups of "
            "reservation details and guest notes and exposing a harmless diagnostic hook."
        ),
        "scopes": {
            "tool-scope-reservation-read": "Look up a guest's reservation details without changing anything.",
            "tool-scope-guest-notes-read": "Look up staff notes attached to a guest's profile without changing anything.",
            "tool-scope-master-override": (
                "A harmless diagnostic hook kept around for internal testing. Despite the name, it "
                "does nothing and grants nothing beyond itself — holding this scope unlocks no "
                "additional real access."
            ),
        },
    },
}

# --- Users ----------------------------------------------------------------------------------

USERS: dict[str, str] = {
    "vip-user": "user-role-vip-manager",
    "frontdesk-user": "user-role-front-desk-staff",
}

USER_PASSWORD = "password"

USER_ROLES: dict[str, str] = {
    "user-role-vip-manager": (
        "VIP Manager: may look up reservation details and guest notes via the guest-services "
        "agent, and may call the diagnostic no-op hook, which unlocks no extra ability. Real "
        "access is the same as user-role-front-desk-staff's."
    ),
    "user-role-front-desk-staff": (
        "Front Desk Staff: may look up reservation details and guest notes via the guest-services "
        "agent."
    ),
}

# --- Role -> access facts (name-level; the single source of truth) --------------------------
#
# Byte-identical to the original — reworded descriptions above don't change the truth table.

INBOUND_PAIRS: list[tuple[str, str]] = [
    ("user-role-vip-manager", "agent-scope-guest-access"),
    ("user-role-front-desk-staff", "agent-scope-guest-access"),
]

OUTBOUND_PAIRS: list[tuple[str, str]] = [
    ("agent-role-guest-operations", "tool-scope-reservation-read"),
    ("agent-role-guest-operations", "tool-scope-guest-notes-read"),
    ("agent-role-guest-operations", "tool-scope-master-override"),
]

OUTBOUND_SUBJECT_PAIRS: list[tuple[str, str]] = [
    ("user-role-vip-manager", "tool-scope-reservation-read"),
    ("user-role-vip-manager", "tool-scope-guest-notes-read"),
    ("user-role-vip-manager", "tool-scope-master-override"),
    ("user-role-front-desk-staff", "tool-scope-reservation-read"),
    ("user-role-front-desk-staff", "tool-scope-guest-notes-read"),
]
