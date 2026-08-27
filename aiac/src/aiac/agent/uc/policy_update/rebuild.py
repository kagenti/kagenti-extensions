"""Policy Update — Rebuild sub-agent (UC2) — stub.

Full authoritative rebuild lands in 3.8. Rebuild is authoritative, so it
returns ``override=True`` (role-keyed replace in the PCE). HTTP-only trigger.

Like Build, Rebuild is allow-only: the ``PolicyRule``s it will emit default to
``RuleEffect.ALLOW`` (deny extraction is deferred), so behavior is unchanged
under the ALLOW/DENY policy-rule model.
"""

from aiac.policy.model.models import PolicyRule


def rebuild_policy() -> tuple[list[PolicyRule], bool]:
    return [], True
