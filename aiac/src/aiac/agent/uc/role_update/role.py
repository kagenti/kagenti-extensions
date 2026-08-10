"""Role sub-agent (UC3) — stub.

Full role-change handling lands in 3.11. A role change is authoritative for
that role, so it returns ``override=True`` (role-keyed replace in the PCE).
"""

from aiac.policy.model.models import PolicyRule


def update_role(role_id: str) -> tuple[list[PolicyRule], bool]:
    return [], True
