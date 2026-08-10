"""Policy Update — Build sub-agent (UC2) — stub.

Full incremental build lands in 3.7. Its ``override`` value is resolved in 6.4;
until then the stub returns ``override=False`` (additive merge).
"""

from aiac.policy.model.models import PolicyRule


def build_policy() -> tuple[list[PolicyRule], bool]:
    return [], False
