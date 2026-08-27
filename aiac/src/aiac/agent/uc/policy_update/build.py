"""Policy Update — Build sub-agent (UC2) — stub.

Full incremental build lands in 3.7. Its ``override`` value is resolved in 6.4;
until then the stub returns ``override=False`` (additive merge).

Build is allow-only: the ``PolicyRule``s it will emit default to
``RuleEffect.ALLOW`` (deny extraction is deferred), so behavior is unchanged
under the ALLOW/DENY policy-rule model.
"""

from aiac.policy.model.models import PolicyRule


def build_policy() -> tuple[list[PolicyRule], bool]:
    return [], False
