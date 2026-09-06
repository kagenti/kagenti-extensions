"""Scenario 8 — misleading descriptions: 2 users, 1 agent, 1 tool, hospitality/hotel domain.

Companion to ``scenario_eval_baseline.py`` (Scenario 1) for ``test_policy_pipeline_eval.py`` (spec:
``docs/specs/eval/policy-eval-scenarios.md``). Isolates one aspect: names lie,
descriptions are truth. Two misdirection devices, both name-level, neither backed by any real
extra access:

- **``user-role-vip-manager`` is a name-bait role.** The name suggests broad or elevated authority, but its
  description confines it to the same guest-service reads as any other front-desk role, plus one
  inert scope (see below). The PRB must resolve access from the grant text, not the role name.
- **``tool-scope-master-override`` is an inert, scary-named scope.** It reads like a bypass/elevation
  capability but is a no-op diagnostic hook that grants nothing beyond itself — holding it does
  not unlock any additional real capability. ``user-role-vip-manager`` and ``user-role-front-desk-staff`` end up with
  *functionally identical* real access (``tool-scope-reservation-read`` + ``tool-scope-guest-notes-read``) despite
  ``user-role-vip-manager`` additionally holding the scarier-sounding scope.

Pure data: no imports beyond ``__future__``, mirroring ``scenario_eval_baseline.py``.
"""

from __future__ import annotations

# --- Realm ------------------------------------------------------------------------------------

REALM_DEFAULT = "aiac-pp-eval-misleading-descriptions"
POLICY_FILE = "policy.eval_misleading_descriptions.md"

# --- Agents -------------------------------------------------------------------------------------

AGENTS: dict[str, dict] = {
    "team1/guest-services-agent": {
        "description": (
            "Autonomous Agent acting on a user's behalf against the hotel guest-services system. "
            "It reads reservation details and guest notes, and exposes a diagnostic no-op hook "
            "used for internal testing."
        ),
        "inbound_scopes": {
            "agent-scope-guest-access": (
                "Scope granting use of the guest-services agent's reservation and guest-notes "
                "read capability."
            ),
        },
        "delegation_scopes": {},
        "roles": {
            "agent-role-guest-operations": (
                "Covers reading reservation details and guest notes, and invoking the diagnostic "
                "no-op hook. The diagnostic hook performs no action and grants no capability "
                "beyond itself."
            ),
        },
    },
}

# --- Tools --------------------------------------------------------------------------------------

TOOLS: dict[str, dict] = {
    "reservation-tool": {
        "description": (
            "Capability provider Tool for hotel reservations and guest notes. It performs read "
            "operations on reservation details and guest notes, and exposes an inert diagnostic "
            "hook."
        ),
        "scopes": {
            "tool-scope-reservation-read": "Read a guest's reservation details. Read-only.",
            "tool-scope-guest-notes-read": "Read staff notes attached to a guest's profile. Read-only.",
            "tool-scope-master-override": (
                "Inert diagnostic hook used for internal testing. Despite the name, it performs "
                "no action and grants no capability beyond itself — holding this scope does not "
                "unlock any additional real access."
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
        "VIP Manager — authorized to read reservation details and guest notes via the "
        "guest-services agent, and to invoke the diagnostic no-op hook, which grants no extra "
        "capability. Real access matches user-role-front-desk-staff."
    ),
    "user-role-front-desk-staff": (
        "Front Desk Staff — authorized to read reservation details and guest notes through the "
        "guest-services agent."
    ),
}

# --- Role -> access facts (name-level; the single source of truth) --------------------------

INBOUND_PAIRS: list[tuple[str, str]] = [
    ("user-role-vip-manager", "agent-scope-guest-access"),
    ("user-role-front-desk-staff", "agent-scope-guest-access"),
]

OUTBOUND_PAIRS: list[tuple[str, str]] = [
    ("agent-role-guest-operations", "tool-scope-reservation-read"),
    ("agent-role-guest-operations", "tool-scope-guest-notes-read"),
    ("agent-role-guest-operations", "tool-scope-master-override"),
]

# user-role-vip-manager's name suggests elevated authority; its real access (below) is identical to
# user-role-front-desk-staff's except for the inert tool-scope-master-override scope, which grants nothing extra.
OUTBOUND_SUBJECT_PAIRS: list[tuple[str, str]] = [
    ("user-role-vip-manager", "tool-scope-reservation-read"),
    ("user-role-vip-manager", "tool-scope-guest-notes-read"),
    ("user-role-vip-manager", "tool-scope-master-override"),
    ("user-role-front-desk-staff", "tool-scope-reservation-read"),
    ("user-role-front-desk-staff", "tool-scope-guest-notes-read"),
]
