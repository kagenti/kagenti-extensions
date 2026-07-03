"""Unit tests for aiac.policy.computation.engine.compute_and_apply.

All four downstream dependencies are mocked at the engine's import boundary:
  - Configuration.get_services_by_scope / get_services_by_role / get_subjects_by_role
  - aiac.policy.computation.engine.get_agent_policy / apply_agent_policy   (Policy Store)
  - aiac.policy.computation.engine.apply_policy                           (PDP Policy Writer)
"""

import os
from contextlib import ExitStack
from unittest.mock import patch

from aiac.idp.configuration.api import Configuration
from aiac.idp.configuration.models import Role, Scope, Service, Subject
from aiac.policy.model.models import AgentPolicyModel, PolicyModel, PolicyRule


# --------------------------------------------------------------------------- #
# builders (mirror test/policy/model/test_models.py)                          #
# --------------------------------------------------------------------------- #
def _role(id="r-edit", name="editor", composite=False, children=None) -> Role:
    return Role(id=id, name=name, composite=composite, childRoles=children or [])


def _scope(id="s-write", name="write") -> Scope:
    return Scope(id=id, name=name)


def _service(service_id, id=None, enabled=True) -> Service:
    return Service(id=id or f"uuid-{service_id}", serviceId=service_id, enabled=enabled)


def _subject(username, id=None, enabled=True) -> Subject:
    return Subject(id=id or f"uuid-{username}", username=username, enabled=enabled)


def _rule(role=None, scope=None) -> PolicyRule:
    return PolicyRule(role=role if role is not None else _role(), scope=scope if scope is not None else _scope())


def _fresh(agent_id) -> AgentPolicyModel:
    return AgentPolicyModel(
        agent_id=agent_id, agent_roles=[], agent_scopes=[],
        source_roles={}, subject_roles={}, target_scopes={},
        inbound_rules=[], outbound_rules=[],
    )


# --------------------------------------------------------------------------- #
# harness                                                                     #
# --------------------------------------------------------------------------- #
def _lookup(mapping):
    """side_effect that returns a copy of mapping[obj.id] (default [])."""
    return lambda obj: list(mapping.get(obj.id, []))


class _Result:
    def __init__(self, aap, ap, gsbs, gsbr, gsubr, gap, order):
        self.apply_agent_policy = aap
        self.apply_policy = ap
        self.get_services_by_scope = gsbs
        self.get_services_by_role = gsbr
        self.get_subjects_by_role = gsubr
        self.get_agent_policy = gap
        self.order = order

    @property
    def written(self):
        """{agent_id: AgentPolicyModel} captured from apply_agent_policy calls."""
        return {c.args[0]: c.args[1] for c in self.apply_agent_policy.call_args_list}


def run_engine(rules, *, scope_services=None, role_services=None, role_subjects=None,
               existing=None, missing_404=False, override=False):
    scope_services = scope_services or {}
    role_services = role_services or {}
    role_subjects = role_subjects or {}
    existing = existing or {}
    order = []

    def _get_agent(agent_id):
        if agent_id in existing:
            return existing[agent_id]
        if missing_404:
            raise RuntimeError("Policy Store error 404")
        return _fresh(agent_id)

    def _rec_agent(agent_id, model):
        order.append(("agent", agent_id))

    def _rec_policy(model):
        order.append(("policy",))

    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"AIAC_REALM": "test-realm"}))
        gsbs = stack.enter_context(
            patch.object(Configuration, "get_services_by_scope", side_effect=_lookup(scope_services)))
        gsbr = stack.enter_context(
            patch.object(Configuration, "get_services_by_role", side_effect=_lookup(role_services)))
        gsubr = stack.enter_context(
            patch.object(Configuration, "get_subjects_by_role", side_effect=_lookup(role_subjects)))
        gap = stack.enter_context(
            patch("aiac.policy.computation.engine.get_agent_policy", side_effect=_get_agent))
        aap = stack.enter_context(
            patch("aiac.policy.computation.engine.apply_agent_policy", side_effect=_rec_agent))
        ap = stack.enter_context(
            patch("aiac.policy.computation.engine.apply_policy", side_effect=_rec_policy))
        from aiac.policy.computation.engine import compute_and_apply
        compute_and_apply(rules, override=override)
        return _Result(aap, ap, gsbs, gsbr, gsubr, gap, order)


# --------------------------------------------------------------------------- #
# Cycle 1 — tracer: a rule whose scope resolves to one target service gets an #
# inbound rule on that target's model, and the PDP is pushed exactly once.    #
# --------------------------------------------------------------------------- #
def test_scope_resolves_to_target_inbound_and_pushes_once():
    rule = _rule(role=_role(), scope=_scope("s-write"))
    res = run_engine([rule], scope_services={"s-write": [_service("github-tool")]})

    assert set(res.written) == {"github-tool"}
    assert res.written["github-tool"].inbound_rules == [rule]
    assert res.apply_policy.call_count == 1


# --------------------------------------------------------------------------- #
# Cycle 2 — a rule whose role resolves to a source service gets an outbound    #
# rule on that source's model, plus a target_scopes entry per resolved target. #
# --------------------------------------------------------------------------- #
def test_role_resolves_to_source_outbound_and_target_scopes():
    role = _role("r-edit")
    scope = _scope("s-write")
    rule = _rule(role=role, scope=scope)
    res = run_engine(
        [rule],
        scope_services={"s-write": [_service("github-tool")]},
        role_services={"r-edit": [_service("weather-agent")]},
    )

    src = res.written["weather-agent"]
    assert src.outbound_rules == [rule]
    assert src.target_scopes == {"github-tool": [scope]}


# --------------------------------------------------------------------------- #
# Cycle 3 — the target model records which source services (by serviceId) can #
# reach it, under source_roles, with the typed Role in the value list.        #
# --------------------------------------------------------------------------- #
def test_target_records_source_roles_by_source_service_id():
    role = _role("r-edit")
    rule = _rule(role=role, scope=_scope("s-write"))
    res = run_engine(
        [rule],
        scope_services={"s-write": [_service("github-tool")]},
        role_services={"r-edit": [_service("weather-agent")]},
    )

    assert res.written["github-tool"].source_roles == {"weather-agent": [role]}


# --------------------------------------------------------------------------- #
# Cycle 4 — get_subjects_by_role is consulted for the rule's role, and the     #
# target model records subject_roles keyed by the subject's username.          #
# --------------------------------------------------------------------------- #
def test_target_records_subject_roles_by_username():
    role = _role("r-edit")
    rule = _rule(role=role, scope=_scope("s-write"))
    res = run_engine(
        [rule],
        scope_services={"s-write": [_service("github-tool")]},
        role_subjects={"r-edit": [_subject("alice")]},
    )

    assert res.written["github-tool"].subject_roles == {"alice": [role]}
    res.get_subjects_by_role.assert_called_once_with(role)


# --------------------------------------------------------------------------- #
# Cycle 5 — the PCE does NOT flatten: rules arrive pre-flattened from the UC,   #
# so a rule carrying a composite role queries the IdP exactly once with that    #
# role as-is — never once per child.                                            #
# --------------------------------------------------------------------------- #
def test_composite_role_is_not_flattened():
    child_a = _role("r-a", "reader")
    child_b = _role("r-b", "writer")
    composite = _role("r-comp", "editor", composite=True, children=[child_a, child_b])
    rule = _rule(role=composite, scope=_scope("s-write"))

    res = run_engine([rule], scope_services={"s-write": [_service("github-tool")]})

    res.get_services_by_role.assert_called_once_with(composite)
    res.get_subjects_by_role.assert_called_once_with(composite)
    roles_queried = [c.args[0] for c in res.get_services_by_role.call_args_list]
    subjects_queried = [c.args[0] for c in res.get_subjects_by_role.call_args_list]
    assert child_a not in roles_queried and child_b not in roles_queried
    assert child_a not in subjects_queried and child_b not in subjects_queried


# --------------------------------------------------------------------------- #
# Cycle 6 — a realm-level role (owned by no service) still records its         #
# subjects on the target, but produces no source model / no source_roles.      #
# --------------------------------------------------------------------------- #
def test_realm_level_role_records_subjects_but_no_source():
    role = _role("r-realm")
    rule = _rule(role=role, scope=_scope("s-write"))
    res = run_engine(
        [rule],
        scope_services={"s-write": [_service("github-tool")]},
        role_services={},  # realm-level: owned by no service
        role_subjects={"r-realm": [_subject("alice")]},
    )

    assert set(res.written) == {"github-tool"}  # no source model created
    target = res.written["github-tool"]
    assert target.inbound_rules == [rule]
    assert target.subject_roles == {"alice": [role]}
    assert target.source_roles == {}
    assert target.outbound_rules == []


# --------------------------------------------------------------------------- #
# Cycle 7 — the engine reads each agent's current model from the store and     #
# appends to it; pre-existing rules and map entries survive the merge.         #
# --------------------------------------------------------------------------- #
def test_merge_preserves_existing_store_model():
    prior_rule = _rule(role=_role("r-old"), scope=_scope("s-old"))
    prior = _fresh("github-tool")
    prior.inbound_rules.append(prior_rule)
    prior.source_roles["old-src"] = [_role("r-old")]
    prior.subject_roles["bob"] = [_role("r-old")]

    rule = _rule(role=_role("r-edit"), scope=_scope("s-write"))
    res = run_engine(
        [rule],
        scope_services={"s-write": [_service("github-tool")]},
        role_services={"r-edit": [_service("weather-agent")]},
        role_subjects={"r-edit": [_subject("alice")]},
        existing={"github-tool": prior},
    )

    target = res.written["github-tool"]
    assert prior_rule in target.inbound_rules and rule in target.inbound_rules
    assert set(target.source_roles) == {"old-src", "weather-agent"}
    assert set(target.subject_roles) == {"bob", "alice"}


# --------------------------------------------------------------------------- #
# Cycle 8 — a rule already present (same role.id + scope.id) is not appended   #
# a second time; de-duplication is by value, not object identity.             #
# --------------------------------------------------------------------------- #
def test_duplicate_rule_not_appended_twice():
    prior = _fresh("github-tool")
    prior.inbound_rules.append(PolicyRule(role=_role("r-edit"), scope=_scope("s-write")))

    rule = _rule(role=_role("r-edit"), scope=_scope("s-write"))  # same ids, new object
    res = run_engine(
        [rule],
        scope_services={"s-write": [_service("github-tool")]},
        existing={"github-tool": prior},
    )

    assert len(res.written["github-tool"].inbound_rules) == 1


# --------------------------------------------------------------------------- #
# Cycle 9 — a map entry already present (same entity .id) is not appended a    #
# second time, for source_roles / subject_roles / target_scopes alike.        #
# --------------------------------------------------------------------------- #
def test_duplicate_map_entries_not_appended_twice():
    role = _role("r-edit")
    scope = _scope("s-write")
    target_prior = _fresh("github-tool")
    target_prior.source_roles["weather-agent"] = [_role("r-edit")]
    target_prior.subject_roles["alice"] = [_role("r-edit")]
    source_prior = _fresh("weather-agent")
    source_prior.target_scopes["github-tool"] = [_scope("s-write")]

    rule = _rule(role=role, scope=scope)
    res = run_engine(
        [rule],
        scope_services={"s-write": [_service("github-tool")]},
        role_services={"r-edit": [_service("weather-agent")]},
        role_subjects={"r-edit": [_subject("alice")]},
        existing={"github-tool": target_prior, "weather-agent": source_prior},
    )

    target = res.written["github-tool"]
    source = res.written["weather-agent"]
    assert len(target.source_roles["weather-agent"]) == 1
    assert len(target.subject_roles["alice"]) == 1
    assert len(source.target_scopes["github-tool"]) == 1


# --------------------------------------------------------------------------- #
# Cycle 10 — when the store has no record for an agent (404), the engine       #
# starts from a fresh model rather than crashing.                             #
# --------------------------------------------------------------------------- #
def test_missing_agent_404_creates_fresh_model():
    rule = _rule(role=_role(), scope=_scope("s-write"))
    res = run_engine(
        [rule],
        scope_services={"s-write": [_service("github-tool")]},
        missing_404=True,  # get_agent_policy raises RuntimeError("...404")
    )

    assert set(res.written) == {"github-tool"}
    assert res.written["github-tool"].inbound_rules == [rule]
    assert res.apply_policy.call_count == 1


# --------------------------------------------------------------------------- #
# Cycle 11 — any dependency failure is logged and swallowed; compute_and_apply #
# never propagates (fire-and-forget), and nothing is pushed to the PDP.       #
# --------------------------------------------------------------------------- #
def test_dependency_exception_is_swallowed():
    from aiac.policy.computation.engine import compute_and_apply

    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"AIAC_REALM": "test-realm"}))
        stack.enter_context(
            patch.object(Configuration, "get_services_by_scope", side_effect=RuntimeError("boom")))
        stack.enter_context(patch("aiac.policy.computation.engine.get_agent_policy"))
        stack.enter_context(patch("aiac.policy.computation.engine.apply_agent_policy"))
        ap = stack.enter_context(patch("aiac.policy.computation.engine.apply_policy"))

        assert compute_and_apply([_rule()]) is None  # must not raise
        ap.assert_not_called()


# --------------------------------------------------------------------------- #
# Cycle 12 — every store write happens before the single PDP push, and the     #
# pushed PolicyModel round-trips through JSON (all map keys are strings).       #
# --------------------------------------------------------------------------- #
def test_writes_precede_single_push_and_model_is_json_serializable():
    rule = _rule(role=_role("r-edit"), scope=_scope("s-write"))
    res = run_engine(
        [rule],
        scope_services={"s-write": [_service("github-tool")]},
        role_services={"r-edit": [_service("weather-agent")]},
        role_subjects={"r-edit": [_subject("alice")]},
    )

    assert res.order.count(("policy",)) == 1
    assert res.order[-1] == ("policy",)
    assert res.order[:-1] and all(step[0] == "agent" for step in res.order[:-1])

    pushed = res.apply_policy.call_args.args[0]
    restored = PolicyModel.model_validate(pushed.model_dump(mode="json"))
    assert {a.agent_id for a in restored.agents} == {"github-tool", "weather-agent"}


# --------------------------------------------------------------------------- #
# Cycle 13 — override=True authoritatively replaces the input role's mappings: #
# stale entries for that role are purged from BOTH directions (and dropped     #
# from source_roles / subject_roles, with target_scopes reconciled) before the #
# fresh rules are appended; an unrelated role's mappings survive untouched.    #
# --------------------------------------------------------------------------- #
def test_override_purges_input_role_before_appending():
    edit = _role("r-edit")
    keep = _role("r-keep")

    target_prior = _fresh("github-tool")
    target_prior.inbound_rules.append(PolicyRule(role=edit, scope=_scope("s-stale")))
    target_prior.inbound_rules.append(PolicyRule(role=keep, scope=_scope("s-keep")))
    target_prior.source_roles["weather-agent"] = [edit, keep]
    target_prior.subject_roles["alice"] = [edit, keep]

    source_prior = _fresh("weather-agent")
    source_prior.outbound_rules.append(PolicyRule(role=edit, scope=_scope("s-stale")))
    source_prior.target_scopes["github-tool"] = [_scope("s-stale")]

    rule = _rule(role=edit, scope=_scope("s-write"))
    res = run_engine(
        [rule],
        scope_services={"s-write": [_service("github-tool")]},
        role_services={"r-edit": [_service("weather-agent")]},
        existing={"github-tool": target_prior, "weather-agent": source_prior},
        override=True,
    )

    target = res.written["github-tool"]
    source = res.written["weather-agent"]

    target_inbound = {(r.role.id, r.scope.id) for r in target.inbound_rules}
    assert ("r-edit", "s-stale") not in target_inbound  # stale purged
    assert ("r-edit", "s-write") in target_inbound  # fresh applied
    assert ("r-keep", "s-keep") in target_inbound  # unrelated role survives

    # r-edit dropped from source_roles/subject_roles then re-added only where
    # the fresh resolution puts it back (source_roles, not subject_roles here).
    assert {r.id for r in target.source_roles["weather-agent"]} == {"r-keep", "r-edit"}
    assert {r.id for r in target.subject_roles["alice"]} == {"r-keep"}

    source_outbound = {(r.role.id, r.scope.id) for r in source.outbound_rules}
    assert ("r-edit", "s-stale") not in source_outbound  # stale purged
    assert ("r-edit", "s-write") in source_outbound  # fresh applied
    # target_scopes reconciled: only scopes justified by surviving outbound rules.
    assert {s.id for s in source.target_scopes["github-tool"]} == {"s-write"}


# --------------------------------------------------------------------------- #
# Cycle 14 — override=True purges each input role ONCE, up-front: two input    #
# rules sharing a role must both land, i.e. the shared role's purge does not    #
# wipe the first rule's mapping after the second is processed.                  #
# --------------------------------------------------------------------------- #
def test_override_shared_role_purged_once_up_front():
    edit = _role("r-edit")

    source_prior = _fresh("weather-agent")
    source_prior.outbound_rules.append(PolicyRule(role=edit, scope=_scope("s-old")))
    source_prior.target_scopes["tool-old"] = [_scope("s-old")]

    rules = [_rule(role=edit, scope=_scope("s-a")), _rule(role=edit, scope=_scope("s-b"))]
    res = run_engine(
        rules,
        scope_services={"s-a": [_service("tool-a")], "s-b": [_service("tool-b")]},
        role_services={"r-edit": [_service("weather-agent")]},
        existing={"weather-agent": source_prior},
        override=True,
    )

    source = res.written["weather-agent"]
    out_pairs = {(r.role.id, r.scope.id) for r in source.outbound_rules}
    assert out_pairs == {("r-edit", "s-a"), ("r-edit", "s-b")}  # both survive; s-old purged once
