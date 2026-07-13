"""Unit tests for aiac.policy.computation.engine.compute_and_apply.

The engine routes each pre-flattened PolicyRule by kind (P5b):

    (user role,  agent scope) -> inbound_rules                    [mapping a]
    (user role,  tool scope)  -> outbound_subject_rules           [mapping b]
    (agent role, tool scope)  -> outbound_rules + target_scopes   [mapping c]

Role kind is read from service ownership (an agent role is owned by an Agent
service; a user role is realm-level). Scope kind is read from the exposing
service's type. Only Agent services are ever modelled (P4); each written model
embeds its own service-account roles/scopes (P2).

All downstream dependencies are mocked at the engine's import boundary:
  - Configuration.get_services (the catalog: type + own roles/scopes)
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
# builders                                                                    #
# --------------------------------------------------------------------------- #
def _role(id="r-edit", name="editor", composite=False, children=None, aiac_managed=True) -> Role:
    # Roles in these tests model AIAC-provisioned agent roles, so they carry the marker by
    # default; pass aiac_managed=False to simulate a Keycloak built-in (e.g. default-roles-<realm>).
    attributes = {"aiac.managed": ["true"]} if aiac_managed else {}
    return Role(id=id, name=name, composite=composite, childRoles=children or [], attributes=attributes)


def _scope(id="s-write", name="write", aiac_managed=True) -> Scope:
    # AIAC-provisioned by default; pass aiac_managed=False for a Keycloak built-in (e.g. profile).
    attributes = {"aiac.managed": "true"} if aiac_managed else {}
    return Scope(id=id, name=name, attributes=attributes)


def _service(service_id, id=None, enabled=True, type=None, roles=None, scopes=None) -> Service:
    return Service(
        id=id or f"uuid-{service_id}",
        serviceId=service_id,
        enabled=enabled,
        type=type,
        roles=roles or [],
        scopes=scopes or [],
    )


def _agent(service_id, roles=None, scopes=None) -> Service:
    return _service(service_id, type="Agent", roles=roles, scopes=scopes)


def _tool(service_id, roles=None, scopes=None) -> Service:
    return _service(service_id, type="Tool", roles=roles, scopes=scopes)


def _subject(username, id=None, enabled=True) -> Subject:
    return Subject(id=id or f"uuid-{username}", username=username, enabled=enabled)


def _rule(role=None, scope=None) -> PolicyRule:
    return PolicyRule(role=role if role is not None else _role(), scope=scope if scope is not None else _scope())


def _fresh(agent_id) -> AgentPolicyModel:
    return AgentPolicyModel(
        agent_id=agent_id, agent_roles=[], agent_scopes=[],
        source_roles={}, subject_roles={}, target_scopes={},
        inbound_rules=[], outbound_rules=[], outbound_subject_rules=[],
    )


# --------------------------------------------------------------------------- #
# harness                                                                     #
# --------------------------------------------------------------------------- #
def _lookup(mapping):
    """side_effect that returns a copy of mapping[obj.id] (default [])."""
    return lambda obj: list(mapping.get(obj.id, []))


class _Result:
    def __init__(self, aap, ap, gs, gsbs, gsbr, gsubr, gap, order):
        self.apply_agent_policy = aap
        self.apply_policy = ap
        self.get_services = gs
        self.get_services_by_scope = gsbs
        self.get_services_by_role = gsbr
        self.get_subjects_by_role = gsubr
        self.get_agent_policy = gap
        self.order = order

    @property
    def written(self):
        """{agent_id: AgentPolicyModel} captured from apply_agent_policy calls."""
        return {c.args[0]: c.args[1] for c in self.apply_agent_policy.call_args_list}


def run_engine(rules, *, catalog=None, scope_services=None, role_services=None,
               role_subjects=None, existing=None, missing_404=False, override=False):
    catalog = catalog or []
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
        gs = stack.enter_context(
            patch.object(Configuration, "get_services", return_value=list(catalog)))
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
        return _Result(aap, ap, gs, gsbs, gsbr, gsubr, gap, order)


# --------------------------------------------------------------------------- #
# Cycle 1 — tracer (mapping a): a (user role, agent scope) rule lands in the   #
# agent's inbound_rules, and the PDP is pushed exactly once.                   #
# --------------------------------------------------------------------------- #
def test_user_role_agent_scope_lands_inbound_and_pushes_once():
    agent = _agent("github-agent")
    rule = _rule(role=_role("r-dev", "developer"), scope=_scope("s-src-access", "source-access"))
    res = run_engine(
        [rule],
        catalog=[agent],
        scope_services={"s-src-access": [agent]},  # agent exposes the agent scope
        role_services={},                           # developer is realm-level (user role)
    )

    assert set(res.written) == {"github-agent"}
    assert res.written["github-agent"].inbound_rules == [rule]
    assert res.apply_policy.call_count == 1


# --------------------------------------------------------------------------- #
# Cycle 2 — mapping c: a (agent role, tool scope) rule lands in the agent's    #
# outbound_rules, plus a target_scopes entry per resolved tool.                #
# --------------------------------------------------------------------------- #
def test_agent_role_tool_scope_lands_outbound_and_target_scopes():
    agent = _agent("github-agent")
    tool = _tool("github-tool")
    role = _role("r-src-helper", "source-helper")
    scope = _scope("s-src-read", "source-read")
    rule = _rule(role=role, scope=scope)
    res = run_engine(
        [rule],
        catalog=[agent, tool],
        scope_services={"s-src-read": [tool]},       # tool exposes the tool scope
        role_services={"r-src-helper": [agent]},     # agent owns the agent role
    )

    m = res.written["github-agent"]
    assert m.outbound_rules == [rule]
    assert m.target_scopes == {"github-tool": [scope]}
    assert set(res.written) == {"github-agent"}      # P4: no tool model


# --------------------------------------------------------------------------- #
# Cycle 3 — mapping b: a (user role, tool scope) rule lands in the             #
# outbound_subject_rules of the agent that targets that tool (established by    #
# a mapping-c rule in the same batch).                                          #
# --------------------------------------------------------------------------- #
def test_user_role_tool_scope_lands_outbound_subject():
    agent = _agent("github-agent")
    tool = _tool("github-tool")
    dev = _role("r-dev", "developer")
    helper = _role("r-src-helper", "source-helper")
    read = _scope("s-src-read", "source-read")
    c_rule = _rule(role=helper, scope=read)   # (c) establishes agent -> tool target_scopes
    b_rule = _rule(role=dev, scope=read)       # (b) user -> tool scope
    res = run_engine(
        [c_rule, b_rule],
        catalog=[agent, tool],
        scope_services={"s-src-read": [tool]},
        role_services={"r-src-helper": [agent]},   # helper owned by agent; dev realm-level
    )

    m = res.written["github-agent"]
    assert m.outbound_subject_rules == [b_rule]
    assert set(res.written) == {"github-agent"}


# --------------------------------------------------------------------------- #
# Cycle 4 — a (user role, tool scope) rule with NO agent targeting that tool    #
# produces no model at all (it cannot be attached anywhere).                    #
# --------------------------------------------------------------------------- #
def test_user_role_tool_scope_without_targeting_agent_drops():
    tool = _tool("github-tool")
    rule = _rule(role=_role("r-dev", "developer"), scope=_scope("s-src-read", "source-read"))
    res = run_engine(
        [rule],
        catalog=[tool],
        scope_services={"s-src-read": [tool]},
        role_services={},  # developer realm-level; no agent owns any role here
    )

    assert res.written == {}


# --------------------------------------------------------------------------- #
# Cycle 5 — P2: each written agent embeds its own service-account roles and     #
# exposed scopes, read from the service catalog.                                #
# --------------------------------------------------------------------------- #
def test_agent_roles_and_scopes_populated_from_catalog():
    helper = _role("r-src-helper", "source-helper")
    ihelper = _role("r-iss-helper", "issues-helper")
    src_access = _scope("s-src-access", "source-access")
    iss_access = _scope("s-iss-access", "issues-access")
    agent = _agent("github-agent", roles=[helper, ihelper], scopes=[src_access, iss_access])
    rule = _rule(role=_role("r-dev", "developer"), scope=src_access)
    res = run_engine([rule], catalog=[agent], scope_services={"s-src-access": [agent]})

    m = res.written["github-agent"]
    assert m.agent_roles == [helper, ihelper]
    assert m.agent_scopes == [src_access, iss_access]


# --------------------------------------------------------------------------- #
# Cycle 6 — a modelled agent with no own roles/scopes in the catalog keeps [].  #
# --------------------------------------------------------------------------- #
def test_agent_without_catalog_roles_scopes_keeps_empty():
    agent = _agent("github-agent")  # no roles, no scopes
    rule = _rule(role=_role("r-dev", "developer"), scope=_scope("s-src-access", "source-access"))
    res = run_engine([rule], catalog=[agent], scope_services={"s-src-access": [agent]})

    m = res.written["github-agent"]
    assert m.agent_roles == []
    assert m.agent_scopes == []


# --------------------------------------------------------------------------- #
# Cycle 6b — P2: only AIAC-provisioned roles/scopes (carrying the aiac.managed   #
# marker) are embedded; Keycloak built-ins (default-roles-<realm>, profile, ...) #
# are dropped from the agent's embed.                                           #
# --------------------------------------------------------------------------- #
def test_builtin_roles_and_scopes_are_filtered_from_embed():
    helper = _role("r-src-helper", "source-helper")                      # AIAC-provisioned
    default_roles = _role("r-def", "default-roles-aiac", aiac_managed=False)  # Keycloak built-in
    src_access = _scope("s-src-access", "source-access")                 # AIAC-provisioned
    profile = _scope("s-profile", "profile", aiac_managed=False)         # Keycloak built-in
    agent = _agent(
        "github-agent",
        roles=[helper, default_roles],
        scopes=[src_access, profile],
    )
    rule = _rule(role=_role("r-dev", "developer"), scope=src_access)
    res = run_engine([rule], catalog=[agent], scope_services={"s-src-access": [agent]})

    m = res.written["github-agent"]
    assert m.agent_roles == [helper]        # default-roles-<realm> dropped
    assert m.agent_scopes == [src_access]   # profile dropped


# --------------------------------------------------------------------------- #
# Cycle 7 — P4: a pure-target Tool service is never modelled, even though it     #
# exposes the scope the agent reaches; the agent -> tool target_scopes edge      #
# still records the tool.                                                        #
# --------------------------------------------------------------------------- #
def test_pure_target_tool_is_not_modelled():
    agent = _agent("github-agent")
    tool = _tool("github-tool")
    rule = _rule(role=_role("r-src-helper", "source-helper"), scope=_scope("s-src-read", "source-read"))
    res = run_engine(
        [rule],
        catalog=[agent, tool],
        scope_services={"s-src-read": [tool]},
        role_services={"r-src-helper": [agent]},
    )

    assert "github-tool" not in res.written
    assert res.written["github-agent"].target_scopes == {"github-tool": [rule.scope]}


# --------------------------------------------------------------------------- #
# Cycle 8 — a (user role, agent scope) rule records the role's subjects on the  #
# agent, keyed by username.                                                     #
# --------------------------------------------------------------------------- #
def test_inbound_records_subject_roles_by_username():
    agent = _agent("github-agent")
    dev = _role("r-dev", "developer")
    rule = _rule(role=dev, scope=_scope("s-src-access", "source-access"))
    res = run_engine(
        [rule],
        catalog=[agent],
        scope_services={"s-src-access": [agent]},
        role_subjects={"r-dev": [_subject("dev-user")]},
    )

    assert res.written["github-agent"].subject_roles == {"dev-user": [dev]}
    res.get_subjects_by_role.assert_called_with(dev)


# --------------------------------------------------------------------------- #
# Cycle 9 — the PCE does NOT flatten: a rule carrying a composite role queries   #
# the IdP exactly once with that role as-is — never once per child.             #
# --------------------------------------------------------------------------- #
def test_composite_role_is_not_flattened():
    child_a = _role("r-a", "reader")
    child_b = _role("r-b", "writer")
    composite = _role("r-comp", "editor", composite=True, children=[child_a, child_b])
    agent = _agent("github-agent")
    rule = _rule(role=composite, scope=_scope("s-src-access", "source-access"))

    res = run_engine(
        [rule],
        catalog=[agent],
        scope_services={"s-src-access": [agent]},
    )

    res.get_services_by_role.assert_called_once_with(composite)
    roles_queried = [c.args[0] for c in res.get_services_by_role.call_args_list]
    assert child_a not in roles_queried and child_b not in roles_queried


# --------------------------------------------------------------------------- #
# Cycle 10 — the engine reads each agent's current model from the store and     #
# appends to it; pre-existing rules survive the merge.                          #
# --------------------------------------------------------------------------- #
def test_merge_preserves_existing_store_model():
    agent = _agent("github-agent")
    prior_rule = _rule(role=_role("r-old", "old"), scope=_scope("s-old-access", "old-access"))
    prior = _fresh("github-agent")
    prior.inbound_rules.append(prior_rule)

    dev = _role("r-dev", "developer")
    rule = _rule(role=dev, scope=_scope("s-src-access", "source-access"))
    res = run_engine(
        [rule],
        catalog=[agent],
        scope_services={"s-src-access": [agent]},
        existing={"github-agent": prior},
    )

    m = res.written["github-agent"]
    assert prior_rule in m.inbound_rules and rule in m.inbound_rules


# --------------------------------------------------------------------------- #
# Cycle 11 — a rule already present (same role.id + scope.id) is not appended    #
# a second time; de-duplication is by value, not object identity.               #
# --------------------------------------------------------------------------- #
def test_duplicate_rule_not_appended_twice():
    agent = _agent("github-agent")
    prior = _fresh("github-agent")
    prior.inbound_rules.append(
        PolicyRule(role=_role("r-dev", "developer"), scope=_scope("s-src-access", "source-access"))
    )

    rule = _rule(role=_role("r-dev", "developer"), scope=_scope("s-src-access", "source-access"))
    res = run_engine(
        [rule],
        catalog=[agent],
        scope_services={"s-src-access": [agent]},
        existing={"github-agent": prior},
    )

    assert len(res.written["github-agent"].inbound_rules) == 1


# --------------------------------------------------------------------------- #
# Cycle 12 — when the store has no record for an agent (404), the engine        #
# starts from a fresh model rather than crashing.                              #
# --------------------------------------------------------------------------- #
def test_missing_agent_404_creates_fresh_model():
    agent = _agent("github-agent")
    rule = _rule(role=_role("r-dev", "developer"), scope=_scope("s-src-access", "source-access"))
    res = run_engine(
        [rule],
        catalog=[agent],
        scope_services={"s-src-access": [agent]},
        missing_404=True,
    )

    assert set(res.written) == {"github-agent"}
    assert res.written["github-agent"].inbound_rules == [rule]
    assert res.apply_policy.call_count == 1


# --------------------------------------------------------------------------- #
# Cycle 13 — any dependency failure is logged and swallowed; compute_and_apply  #
# never propagates (fire-and-forget), and nothing is pushed to the PDP.        #
# --------------------------------------------------------------------------- #
def test_dependency_exception_is_swallowed():
    from aiac.policy.computation.engine import compute_and_apply

    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"AIAC_REALM": "test-realm"}))
        stack.enter_context(
            patch.object(Configuration, "get_services", side_effect=RuntimeError("boom")))
        stack.enter_context(patch("aiac.policy.computation.engine.get_agent_policy"))
        stack.enter_context(patch("aiac.policy.computation.engine.apply_agent_policy"))
        ap = stack.enter_context(patch("aiac.policy.computation.engine.apply_policy"))

        assert compute_and_apply([_rule()]) is None  # must not raise
        ap.assert_not_called()


# --------------------------------------------------------------------------- #
# Cycle 14 — every store write happens before the single PDP push, and the      #
# pushed PolicyModel round-trips through JSON (including outbound_subject_rules).#
# --------------------------------------------------------------------------- #
def test_writes_precede_single_push_and_model_is_json_serializable():
    agent = _agent("github-agent")
    tool = _tool("github-tool")
    dev = _role("r-dev", "developer")
    helper = _role("r-src-helper", "source-helper")
    read = _scope("s-src-read", "source-read")
    src_access = _scope("s-src-access", "source-access")
    rules = [
        _rule(role=dev, scope=src_access),   # (a)
        _rule(role=helper, scope=read),      # (c) establishes target_scopes
        _rule(role=dev, scope=read),         # (b) user -> tool scope
    ]
    res = run_engine(
        rules,
        catalog=[agent, tool],
        scope_services={"s-src-access": [agent], "s-src-read": [tool]},
        role_services={"r-src-helper": [agent]},
        role_subjects={"r-dev": [_subject("dev-user")]},
    )

    assert res.order.count(("policy",)) == 1
    assert res.order[-1] == ("policy",)
    assert res.order[:-1] and all(step[0] == "agent" for step in res.order[:-1])

    m = res.written["github-agent"]
    assert m.outbound_subject_rules  # non-empty (mapping b landed)

    pushed = res.apply_policy.call_args.args[0]
    restored = PolicyModel.model_validate(pushed.model_dump(mode="json"))
    assert {a.agent_id for a in restored.agents} == {"github-agent"}
    restored_agent = restored.agents[0]
    assert {(r.role.id, r.scope.id) for r in restored_agent.outbound_subject_rules} == {("r-dev", "s-src-read")}


# --------------------------------------------------------------------------- #
# Cycle 15 — override=True authoritatively replaces the input role's mappings   #
# across inbound_rules, outbound_rules, AND outbound_subject_rules before the    #
# fresh rules are appended; an unrelated role's mappings survive untouched.     #
# --------------------------------------------------------------------------- #
def test_override_purges_input_role_before_appending():
    agent = _agent("github-agent")
    tool = _tool("github-tool")
    dev = _role("r-dev", "developer")
    keep = _role("r-keep", "keeper")
    read = _scope("s-src-read", "source-read")

    prior = _fresh("github-agent")
    prior.outbound_subject_rules.append(PolicyRule(role=dev, scope=_scope("s-stale", "stale")))
    prior.outbound_subject_rules.append(PolicyRule(role=keep, scope=_scope("s-keep", "keep")))
    prior.target_scopes["github-tool"] = [read]

    helper = _role("r-src-helper", "source-helper")
    rules = [
        _rule(role=helper, scope=read),   # (c) re-establishes target_scopes edge
        _rule(role=dev, scope=read),      # (b) fresh user -> tool scope
    ]
    res = run_engine(
        rules,
        catalog=[agent, tool],
        scope_services={"s-src-read": [tool]},
        role_services={"r-src-helper": [agent]},
        existing={"github-agent": prior},
        override=True,
    )

    m = res.written["github-agent"]
    pairs = {(r.role.id, r.scope.id) for r in m.outbound_subject_rules}
    assert ("r-dev", "s-stale") not in pairs   # stale purged
    assert ("r-dev", "s-src-read") in pairs      # fresh applied
    assert ("r-keep", "s-keep") in pairs         # unrelated role survives
