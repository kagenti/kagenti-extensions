"""Unit tests for the Policy Update Build + Rebuild sub-agents.

These sub-agents are keep-green under the ALLOW/DENY policy-rule model (#127):
Build (role-closure flatten -> PRB -> merge) and Rebuild (authoritative, delegates
to Build with ``override=True``) construct/return ``list[PolicyRule]`` and hand
``(rules, override)`` to the Controller, which makes the single PCE
``compute_and_apply`` call. The sub-agents themselves perform no PDP / PCE / store
write. ``PolicyRule.effect`` defaults to ``RuleEffect.ALLOW``, so the sub-agents
stay allow-only and behavior is unchanged; deny extraction remains deferred.

Build and Rebuild are stubs today (full build lands in 3.7 / 3.8), so they return
empty rule lists. The tests lock the return/override contract, the ALLOW-only
shape of any rule they emit, and the no-direct-write invariant.
"""

from unittest.mock import patch

from aiac.agent.uc.policy_update.build import build_policy
from aiac.agent.uc.policy_update.rebuild import rebuild_policy
from aiac.idp.configuration.models import Role, Scope
from aiac.policy.model.models import PolicyRule, RuleEffect


def test_build_rule_shape_defaults_to_allow_effect():
    # The rule shape Build/Rebuild produce — a PolicyRule built from a Role + Scope with
    # no explicit effect — defaults to ALLOW and serializes as "Allow". This locks the
    # allow-only, behavior-unchanged contract the sub-agents rely on.
    rule = PolicyRule(
        role=Role(id="r-1", name="editor", composite=False),
        scope=Scope(id="s-1", name="write"),
    )
    assert rule.effect is RuleEffect.ALLOW
    assert rule.effect == "Allow"


def test_build_policy_returns_allow_only_rules_with_additive_override():
    rules, override = build_policy()

    # Behavior unchanged: Build is an additive/incremental merge, so override is False.
    assert isinstance(rules, list)
    assert (rules, override) == ([], False)
    # Any rule Build emits is an Allow grant — never a Deny (deny extraction stays deferred).
    # Vacuously true for today's empty stub; guards against a Deny slipping in once 3.7 lands.
    assert all(rule.effect is RuleEffect.ALLOW for rule in rules)


def test_rebuild_policy_returns_allow_only_rules_with_authoritative_override():
    rules, override = rebuild_policy()

    # Rebuild is authoritative (role-keyed replace in the PCE), so override is True.
    assert isinstance(rules, list)
    assert (rules, override) == ([], True)
    assert all(rule.effect is RuleEffect.ALLOW for rule in rules)


def test_build_and_rebuild_write_nothing_to_pce_or_store():
    # The sub-agents only compute and return (rules, override); the single PCE call and any
    # store write live in the Controller. Invoking them must touch neither the PCE nor the store.
    with (
        patch("aiac.policy.computation.compute_and_apply") as compute_and_apply,
        patch("aiac.policy.computation.decommission") as decommission,
        patch("aiac.policy.model_store.library.api.apply_service_policy") as apply_spm,
        patch("aiac.policy.model_store.library.api.delete_service_policy") as delete_spm,
        patch("aiac.policy.model_store.library.api.clear_service_policies") as clear_spm,
    ):
        build_policy()
        rebuild_policy()

    for spy in (compute_and_apply, decommission, apply_spm, delete_spm, clear_spm):
        spy.assert_not_called()


def test_build_and_rebuild_modules_do_not_import_a_write_surface():
    # Complements the spy above for the top-level ``from ... import <write_fn>`` case: no PCE or
    # store write symbol may be bound into either sub-agent's module namespace.
    import aiac.agent.uc.policy_update.build as build_module
    import aiac.agent.uc.policy_update.rebuild as rebuild_module

    forbidden = {
        "compute_and_apply",
        "decommission",
        "apply_service_policy",
        "delete_service_policy",
        "clear_service_policies",
    }
    for module in (build_module, rebuild_module):
        assert forbidden.isdisjoint(vars(module)), (
            f"{module.__name__} must not import a PDP/PCE/store write surface"
        )
