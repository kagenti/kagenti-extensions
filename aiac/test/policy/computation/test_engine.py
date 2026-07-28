"""Unit tests for ``aiac.policy.computation.engine.compute_and_apply`` (SPM-based).

The engine routes each pre-flattened ``PolicyRule`` to the ``ServicePolicyModel`` (SPM) of the
service that *owns* the rule's scope (``scope.serviceId``), persists the changed SPMs, computes
the affected-agent set from the batch, then **derives** each affected agent's
``AgentPolicyModel`` (APM) entirely from the SPMs (zero IdP) and partial-upserts them once.

Tests assert external behaviour — what the engine writes to the Policy Store (SPMs) and pushes to
the PDP (derived APMs) — not internal merge logic. All downstream dependencies are mocked at the
engine's import boundary via a small in-memory ``FakeStore`` that behaves like the real Policy
Store library (fresh-empty SPM on 404, ``get_service_policies_by_role`` scanning ``inbound_rules``):
  - ``Configuration.get_services``                          (IdP catalog: type + own roles/scopes)
  - ``engine.get_service_policy`` / ``get_service_policies_by_role`` / ``apply_service_policy``
  - ``engine.apply_policy``                                 (PDP Policy Writer partial upsert)
"""

import os
from contextlib import ExitStack, contextmanager
from unittest.mock import patch

import pytest

from aiac.idp.configuration.api import Configuration
from aiac.idp.configuration.models import Role, RoleKind, Scope, Service, ServiceType
from aiac.policy.model.models import PolicyModel, PolicyRule, ServicePolicyModel


# --------------------------------------------------------------------------- #
# builders                                                                    #
# --------------------------------------------------------------------------- #
def _role(id, name=None, *, kind=RoleKind.USER, actor_ids=None, composite=False,
          children=None, aiac_managed=True) -> Role:
    attributes = {"aiac.managed": ["true"]} if aiac_managed else {}
    return Role(
        id=id, name=name or id, composite=composite, childRoles=children or [],
        attributes=attributes, kind=kind, actorIds=actor_ids or [],
    )


def _user_role(id, name=None, *, users) -> Role:
    return _role(id, name, kind=RoleKind.USER, actor_ids=users)


def _agent_role(id, name=None, *, owner) -> Role:
    return _role(id, name, kind=RoleKind.AGENT, actor_ids=[owner])


def _scope(id, name=None, *, service_id="", aiac_managed=True) -> Scope:
    attributes = {"aiac.managed": "true"} if aiac_managed else {}
    return Scope(id=id, name=name or id, attributes=attributes, serviceId=service_id)


def _service(service_id, *, type=None, roles=None, scopes=None) -> Service:
    return Service(
        id=f"uuid-{service_id}", serviceId=service_id, enabled=True,
        type=type, roles=roles or [], scopes=scopes or [],
    )


def _agent(service_id, *, roles=None, scopes=None) -> Service:
    return _service(service_id, type=ServiceType.AGENT, roles=roles, scopes=scopes)


def _tool(service_id, *, roles=None, scopes=None) -> Service:
    return _service(service_id, type=ServiceType.TOOL, roles=roles, scopes=scopes)


def _rule(role, scope) -> PolicyRule:
    return PolicyRule(role=role, scope=scope)


def _spm(service_id, *, type=ServiceType.AGENT, owned_roles=None, owned_scopes=None,
         inbound=None) -> ServicePolicyModel:
    return ServicePolicyModel(
        service_id=service_id, service_type=type,
        owned_roles=owned_roles or [], owned_scopes=owned_scopes or [],
        inbound_rules=inbound or [],
    )


# --------------------------------------------------------------------------- #
# harness — an in-memory Policy Store behaving like the real library          #
# --------------------------------------------------------------------------- #
class FakeStore:
    def __init__(self, initial=None):
        self.data = {sid: m.model_copy(deep=True) for sid, m in (initial or {}).items()}
        self.service_writes = []   # [(service_id, SPM)] captured from apply_service_policy
        self.by_role_calls = []    # [Role] captured from get_service_policies_by_role
        self.policy_pushes = []    # [PolicyModel] captured from apply_policy
        self.service_deletes = []  # [service_id] captured from delete_service_policy
        self.agent_deletes = []    # [agent_id] captured from delete_agent_policy

    def get_service_policy(self, service_id):
        if service_id in self.data:
            return self.data[service_id].model_copy(deep=True)
        return _spm(service_id)  # real lib returns a fresh empty SPM on 404

    def get_service_policies_by_role(self, role):
        self.by_role_calls.append(role)
        return [
            m.model_copy(deep=True)
            for m in self.data.values()
            if any(r.role.id == role.id for r in m.inbound_rules)
        ]

    def apply_service_policy(self, service_id, spm):
        self.service_writes.append((service_id, spm.model_copy(deep=True)))
        self.data[service_id] = spm.model_copy(deep=True)

    def apply_policy(self, model):
        self.policy_pushes.append(model.model_copy(deep=True))

    def delete_service_policy(self, service_id):
        self.service_deletes.append(service_id)
        self.data.pop(service_id, None)

    def delete_agent_policy(self, agent_id):
        self.agent_deletes.append(agent_id)

    # ---- assertion helpers ------------------------------------------------ #
    @property
    def apply_policy_count(self):
        return len(self.policy_pushes)

    @property
    def last_push(self):
        return self.policy_pushes[-1] if self.policy_pushes else None

    def pushed_agent(self, agent_id):
        """The most recent derived APM for ``agent_id`` across all pushes (last wins)."""
        for push in reversed(self.policy_pushes):
            for apm in push.agents:
                if apm.agent_id == agent_id:
                    return apm
        return None

    @property
    def pushed_agent_ids(self):
        return {a.agent_id for push in self.policy_pushes for a in push.agents}


@contextmanager
def engine_env(catalog, store):
    """Patch the engine boundary; yield ``compute_and_apply``. Multiple calls share the store."""
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"KEYCLOAK_REALM": "test-realm"}))
        stack.enter_context(patch.object(Configuration, "get_services", return_value=list(catalog)))
        stack.enter_context(patch("aiac.policy.computation.engine.get_service_policy",
                                  side_effect=store.get_service_policy))
        stack.enter_context(patch("aiac.policy.computation.engine.get_service_policies_by_role",
                                  side_effect=store.get_service_policies_by_role))
        stack.enter_context(patch("aiac.policy.computation.engine.apply_service_policy",
                                  side_effect=store.apply_service_policy))
        stack.enter_context(patch("aiac.policy.computation.engine.apply_policy",
                                  side_effect=store.apply_policy))
        stack.enter_context(patch("aiac.policy.computation.engine.delete_service_policy",
                                  side_effect=store.delete_service_policy))
        stack.enter_context(patch("aiac.policy.computation.engine.delete_agent_policy",
                                  side_effect=store.delete_agent_policy))
        from aiac.policy.computation.engine import compute_and_apply
        yield compute_and_apply


def run_engine(rules, *, catalog=None, store_initial=None, override=False) -> FakeStore:
    store = FakeStore(store_initial)
    with engine_env(catalog or [], store) as compute_and_apply:
        compute_and_apply(rules, override=override)
    return store


# --------------------------------------------------------------------------- #
# comparison helpers                                                          #
# --------------------------------------------------------------------------- #
def _pairs(rules):
    return sorted((r.role.id, r.scope.id) for r in rules)


def _norm(apm):
    """Order-independent view of an APM for equality assertions."""
    return {
        "agent_roles": sorted(r.id for r in apm.agent_roles),
        "agent_scopes": sorted(s.id for s in apm.agent_scopes),
        "inbound": _pairs(apm.inbound_rules),
        "outbound": _pairs(apm.outbound_rules),
        "outbound_subject": _pairs(apm.outbound_subject_rules),
        "source_roles": {k: sorted(r.id for r in v) for k, v in apm.source_roles.items()},
        "subject_roles": {k: sorted(r.id for r in v) for k, v in apm.subject_roles.items()},
        "target_scopes": {k: sorted(s.id for s in v) for k, v in apm.target_scopes.items()},
    }


# --------------------------------------------------------------------------- #
# shared repro fixture — the order-dependence scenario                        #
#   UR (user role) -> AS (agent A's scope)  and  -> TS (tool T's scope)        #
#   AR (agent A's client role) -> TS                                           #
# --------------------------------------------------------------------------- #
def _repro():
    AR = _agent_role("r-agent-src", "agent-source", owner="github-agent")
    UR = _user_role("r-user-dev", "developer", users=["dev-user"])
    AS = _scope("s-agent-inbound", "agent-inbound", service_id="github-agent")
    TS = _scope("s-tool-read", "tool-read", service_id="github-tool")
    catalog = [
        _agent("github-agent", roles=[AR], scopes=[AS]),
        _tool("github-tool", scopes=[TS]),
    ]
    return AR, UR, AS, TS, catalog


# --------------------------------------------------------------------------- #
# Cycle 1 — tracer: a (user role, agent scope) rule lands as an inbound edge    #
# on SPM(A); A's derived APM carries it inbound; the PDP is pushed once.        #
# --------------------------------------------------------------------------- #
def test_user_role_agent_scope_lands_inbound_and_pushes_once():
    AR, UR, AS, TS, catalog = _repro()
    store = run_engine([_rule(UR, AS)], catalog=catalog)

    # persisted on SPM(github-agent)
    assert store.service_writes[0][0] == "github-agent"
    assert _pairs(store.data["github-agent"].inbound_rules) == [("r-user-dev", "s-agent-inbound")]
    # derived onto the agent's APM
    apm = store.pushed_agent("github-agent")
    assert _pairs(apm.inbound_rules) == [("r-user-dev", "s-agent-inbound")]
    assert apm.subject_roles == {"dev-user": [UR]}
    assert store.apply_policy_count == 1


# --------------------------------------------------------------------------- #
# Cycle 2 — an (agent role, tool scope) rule is stored on SPM(T); A's derived   #
# APM gains outbound_rules + a target_scopes entry for the tool.               #
# --------------------------------------------------------------------------- #
def test_agent_role_tool_scope_derives_outbound_and_target_scopes():
    AR, UR, AS, TS, catalog = _repro()
    store = run_engine([_rule(AR, TS)], catalog=catalog)

    assert _pairs(store.data["github-tool"].inbound_rules) == [("r-agent-src", "s-tool-read")]
    apm = store.pushed_agent("github-agent")
    assert _pairs(apm.outbound_rules) == [("r-agent-src", "s-tool-read")]
    assert {k: [s.id for s in v] for k, v in apm.target_scopes.items()} == {"github-tool": ["s-tool-read"]}


# --------------------------------------------------------------------------- #
# Cycle 3 — a (user role, tool scope) rule, once an agent targets that tool,    #
# becomes the agent's outbound subject gate.                                    #
# --------------------------------------------------------------------------- #
def test_user_role_tool_scope_becomes_outbound_subject_gate():
    AR, UR, AS, TS, catalog = _repro()
    store = run_engine([_rule(AR, TS), _rule(UR, TS)], catalog=catalog)

    apm = store.pushed_agent("github-agent")
    assert _pairs(apm.outbound_subject_rules) == [("r-user-dev", "s-tool-read")]
    assert apm.subject_roles == {"dev-user": [UR]}


# --------------------------------------------------------------------------- #
# Cycle 4 — HEADLINE: both onboarding orders converge to an identical APM(A).   #
# --------------------------------------------------------------------------- #
def test_both_orders_yield_identical_agent_policy():
    AR, UR, AS, TS, catalog = _repro()

    # order A-then-T: agent onboarded (UR->AS), then tool onboarded (AR->TS, UR->TS)
    store_at = FakeStore()
    with engine_env(catalog, store_at) as compute:
        compute([_rule(UR, AS)])
        compute([_rule(AR, TS), _rule(UR, TS)])

    # order T-then-A: tool onboarded first, then agent
    store_ta = FakeStore()
    with engine_env(catalog, store_ta) as compute:
        compute([_rule(AR, TS), _rule(UR, TS)])
        compute([_rule(UR, AS)])

    apm_at = store_at.pushed_agent("github-agent")
    apm_ta = store_ta.pushed_agent("github-agent")
    assert _norm(apm_at) == _norm(apm_ta)

    # and it is the expected policy: inbound {UR->AS}, outbound {AR->TS} + subject gate {UR->TS}
    assert _norm(apm_at)["inbound"] == [("r-user-dev", "s-agent-inbound")]
    assert _norm(apm_at)["outbound"] == [("r-agent-src", "s-tool-read")]
    assert _norm(apm_at)["outbound_subject"] == [("r-user-dev", "s-tool-read")]


# --------------------------------------------------------------------------- #
# Cycle 5 — latent sibling bug: after A+T exist, a late (UR2 -> TS) user-role    #
# rule routes to SPM(T), marks A affected, and A's re-derived subject gate       #
# includes UR2.                                                                  #
# --------------------------------------------------------------------------- #
def test_late_user_role_on_tool_rederives_affected_agent_subject_gate():
    AR, UR, AS, TS, catalog = _repro()
    store = FakeStore()
    with engine_env(catalog, store) as compute:
        compute([_rule(UR, AS), _rule(AR, TS), _rule(UR, TS)])  # A + T established
        UR2 = _user_role("r-user-ops", "ops", users=["ops-user"])
        compute([_rule(UR2, TS)])  # late UC3 user role on the tool

    apm = store.pushed_agent("github-agent")
    subject_pairs = _pairs(apm.outbound_subject_rules)
    assert ("r-user-ops", "s-tool-read") in subject_pairs
    assert "ops-user" in apm.subject_roles


# --------------------------------------------------------------------------- #
# Cycle 6 — agent -> agent (AR -> BS): stored on SPM(B); A's APM has it outbound  #
# + target_scopes[B]; B's APM has source_roles[A] += AR.                        #
# --------------------------------------------------------------------------- #
def test_agent_to_agent_edge_projects_into_both_policies():
    AR = _agent_role("r-a-caller", "a-caller", owner="agent-a")
    BS = _scope("s-b-inbound", "b-inbound", service_id="agent-b")
    catalog = [
        _agent("agent-a", roles=[AR], scopes=[_scope("s-a-inbound", service_id="agent-a")]),
        _agent("agent-b", scopes=[BS]),
    ]
    store = run_engine([_rule(AR, BS)], catalog=catalog)

    apm_a = store.pushed_agent("agent-a")
    assert _pairs(apm_a.outbound_rules) == [("r-a-caller", "s-b-inbound")]
    assert {k: [s.id for s in v] for k, v in apm_a.target_scopes.items()} == {"agent-b": ["s-b-inbound"]}

    apm_b = store.pushed_agent("agent-b")
    assert {k: [r.id for r in v] for k, v in apm_b.source_roles.items()} == {"agent-a": ["r-a-caller"]}


# --------------------------------------------------------------------------- #
# Cycle 7 — override purge across SPMs: an input role present on multiple SPMs   #
# is purged from every one of them, once, before the fresh rule is appended.     #
# --------------------------------------------------------------------------- #
def test_override_purges_input_role_from_every_spm():
    shared = _user_role("r-shared", "shared", users=["u"])
    s1 = _scope("s-one", service_id="svc-one")
    s2 = _scope("s-two", service_id="svc-two")
    catalog = [_agent("svc-one", scopes=[s1]), _agent("svc-two", scopes=[s2])]
    initial = {
        "svc-one": _spm("svc-one", owned_scopes=[s1], inbound=[_rule(shared, s1)]),
        "svc-two": _spm("svc-two", owned_scopes=[s2], inbound=[_rule(shared, s2)]),
    }
    # override with the same role targeting only svc-one now
    store = run_engine([_rule(shared, s1)], catalog=catalog, store_initial=initial, override=True)

    # svc-two's stale mapping for the shared role is gone; svc-one keeps the fresh one
    assert _pairs(store.data["svc-two"].inbound_rules) == []
    assert _pairs(store.data["svc-one"].inbound_rules) == [("r-shared", "s-one")]
    # purge scanned by role, once for the single distinct input role
    assert [r.id for r in store.by_role_calls].count("r-shared") == 1


# --------------------------------------------------------------------------- #
# Cycle 8 — override, two input rules sharing one role: the role is purged once  #
# up-front, so the SECOND rule's freshly-appended mapping is not wiped.          #
# --------------------------------------------------------------------------- #
def test_override_shared_role_purged_once_second_mapping_survives():
    shared = _user_role("r-shared", "shared", users=["u"])
    s1 = _scope("s-one", service_id="svc-one")
    s2 = _scope("s-two", service_id="svc-two")
    catalog = [_agent("svc-one", scopes=[s1]), _agent("svc-two", scopes=[s2])]
    initial = {
        "svc-one": _spm("svc-one", owned_scopes=[s1], inbound=[_rule(shared, s1)]),
        "svc-two": _spm("svc-two", owned_scopes=[s2]),
    }
    store = run_engine(
        [_rule(shared, s1), _rule(shared, s2)],
        catalog=catalog, store_initial=initial, override=True,
    )

    assert _pairs(store.data["svc-one"].inbound_rules) == [("r-shared", "s-one")]
    assert _pairs(store.data["svc-two"].inbound_rules) == [("r-shared", "s-two")]  # not wiped


# --------------------------------------------------------------------------- #
# Cycle 9 — append dedup: a rule already on the target SPM (same role.id +       #
# scope.id) is not appended a second time.                                      #
# --------------------------------------------------------------------------- #
def test_duplicate_rule_not_appended_twice():
    AR, UR, AS, TS, catalog = _repro()
    initial = {"github-agent": _spm("github-agent", owned_scopes=[AS], inbound=[_rule(UR, AS)])}
    store = run_engine([_rule(UR, AS)], catalog=catalog, store_initial=initial)

    assert len(store.data["github-agent"].inbound_rules) == 1


# --------------------------------------------------------------------------- #
# Cycle 10 — no flattening: a composite input role never triggers per-child      #
# get_service_policies_by_role calls.                                            #
# --------------------------------------------------------------------------- #
def test_composite_role_is_not_flattened():
    child_a = _agent_role("r-child-a", "child-a", owner="github-agent")
    child_b = _agent_role("r-child-b", "child-b", owner="github-agent")
    composite = _agent_role("r-comp", "composite", owner="github-agent")
    composite = composite.model_copy(update={"composite": True, "childRoles": [child_a, child_b]})
    TS = _scope("s-tool-read", service_id="github-tool")
    catalog = [_agent("github-agent", roles=[composite]), _tool("github-tool", scopes=[TS])]

    store = run_engine([_rule(composite, TS)], catalog=catalog, override=True)

    queried = {r.id for r in store.by_role_calls}
    assert "r-child-a" not in queried and "r-child-b" not in queried


# --------------------------------------------------------------------------- #
# Cycle 11 — P4: a Tool accrues durable inbound_rules on its SPM but is never     #
# emitted as an APM; the agent's target_scopes edge to the tool still appears.   #
# --------------------------------------------------------------------------- #
def test_tool_gets_spm_but_no_apm():
    AR, UR, AS, TS, catalog = _repro()
    store = run_engine([_rule(AR, TS)], catalog=catalog)

    assert _pairs(store.data["github-tool"].inbound_rules) == [("r-agent-src", "s-tool-read")]
    assert "github-tool" not in store.pushed_agent_ids  # no tool APM
    apm = store.pushed_agent("github-agent")
    assert "github-tool" in apm.target_scopes


# --------------------------------------------------------------------------- #
# Cycle 12 — P2 identity from owned_*: the derived APM embeds the agent's own     #
# aiac.managed roles/scopes; built-ins are filtered; an agent with none keeps []. #
# --------------------------------------------------------------------------- #
def test_p2_identity_embeds_aiac_managed_owned_roles_and_scopes():
    helper = _agent_role("r-helper", "helper", owner="github-agent")
    builtin = _agent_role("r-default", "default-roles-aiac", owner="github-agent")
    builtin = builtin.model_copy(update={"attributes": {}})  # not aiac.managed
    src = _scope("s-src", "source", service_id="github-agent")
    profile = _scope("s-profile", "profile", service_id="github-agent", aiac_managed=False)
    agent = _agent("github-agent", roles=[helper, builtin], scopes=[src, profile])
    UR = _user_role("r-user", users=["u"])
    store = run_engine([_rule(UR, src)], catalog=[agent])

    apm = store.pushed_agent("github-agent")
    assert [r.id for r in apm.agent_roles] == ["r-helper"]      # built-in role dropped
    assert [s.id for s in apm.agent_scopes] == ["s-src"]        # profile scope dropped


def test_p2_identity_empty_when_no_owned_entities():
    agent = _agent("github-agent")  # no catalog roles/scopes
    UR = _user_role("r-user", users=["u"])
    store = run_engine([_rule(UR, _scope("s-x", service_id="github-agent"))], catalog=[agent])

    apm = store.pushed_agent("github-agent")
    assert apm.agent_roles == [] and apm.agent_scopes == []


# --------------------------------------------------------------------------- #
# Cycle 13 — directional relevance: a user role shared between an agent scope     #
# and a tool scope does NOT create a false outbound edge from A to the tool.      #
# --------------------------------------------------------------------------- #
def test_shared_user_role_creates_no_false_outbound_edge():
    UR = _user_role("r-user-dev", "developer", users=["dev-user"])
    AS = _scope("s-agent-inbound", service_id="github-agent")
    TS = _scope("s-tool-read", service_id="github-tool")
    # A owns NO agent role that maps to TS — only the shared user role touches both scopes.
    catalog = [_agent("github-agent", scopes=[AS]), _tool("github-tool", scopes=[TS])]
    store = run_engine([_rule(UR, AS), _rule(UR, TS)], catalog=catalog)

    apm = store.pushed_agent("github-agent")
    assert apm.outbound_rules == []
    assert apm.target_scopes == {}
    assert apm.outbound_subject_rules == []  # A does not target T, so no gate


# --------------------------------------------------------------------------- #
# Cycle 13b — UC-1-shaped multi-role capability match: an agent owning TWO         #
# operator roles reaching four tool scopes, with user edges on a subset, derives   #
# BOTH outbound gates — the full agent->tool outbound_rules + target_scopes, and    #
# the user->tool outbound_subject gate. This is what populates the per-scope AND.   #
# --------------------------------------------------------------------------- #
def test_multi_role_capability_match_populates_both_outbound_gates():
    src_op = _agent_role("r-src-op", "source_operations", owner="github-agent")
    issue_op = _agent_role("r-issue-op", "issue_operations", owner="github-agent")
    developer = _user_role("r-developer", "developer", users=["dev-user"])
    tester = _user_role("r-tester", "tester", users=["test-user"])
    sr = _scope("s-source-read", service_id="github-tool")
    sw = _scope("s-source-write", service_id="github-tool")
    ir = _scope("s-issues-read", service_id="github-tool")
    iw = _scope("s-issues-write", service_id="github-tool")
    catalog = [
        _agent("github-agent", roles=[src_op, issue_op],
               scopes=[_scope("s-agent-inbound", service_id="github-agent")]),
        _tool("github-tool", scopes=[sr, sw, ir, iw]),
    ]
    rules = [
        # capability gate: each operator role -> its domain's tool scopes (capability-match)
        _rule(src_op, sr), _rule(src_op, sw),
        _rule(issue_op, ir), _rule(issue_op, iw),
        # subject gate: user roles -> a subset of the tool scopes
        _rule(developer, sr), _rule(developer, sw), _rule(developer, ir),
        _rule(tester, ir), _rule(tester, iw),
    ]
    store = run_engine(rules, catalog=catalog)

    apm = store.pushed_agent("github-agent")
    # capability gate: all four agent->tool edges + target_scopes covering all four scopes
    assert _pairs(apm.outbound_rules) == sorted([
        ("r-src-op", "s-source-read"), ("r-src-op", "s-source-write"),
        ("r-issue-op", "s-issues-read"), ("r-issue-op", "s-issues-write"),
    ])
    assert {k: sorted(s.id for s in v) for k, v in apm.target_scopes.items()} == {
        "github-tool": ["s-issues-read", "s-issues-write", "s-source-read", "s-source-write"],
    }
    # subject gate: the user->tool grant set (developer: source rw + issues read; tester: issues rw)
    assert _pairs(apm.outbound_subject_rules) == sorted([
        ("r-developer", "s-source-read"), ("r-developer", "s-source-write"),
        ("r-developer", "s-issues-read"),
        ("r-tester", "s-issues-read"), ("r-tester", "s-issues-write"),
    ])


# --------------------------------------------------------------------------- #
# Cycle 14 — affected set from the batch: an agent unrelated to the batch is      #
# never derived or upserted, even though it exists in the catalog/store.          #
# --------------------------------------------------------------------------- #
def test_unrelated_agent_is_not_derived():
    AR, UR, AS, TS, catalog = _repro()
    catalog = catalog + [_agent("other-agent", scopes=[_scope("s-other", service_id="other-agent")])]
    store = run_engine([_rule(UR, AS)], catalog=catalog)

    assert store.pushed_agent_ids == {"github-agent"}


# --------------------------------------------------------------------------- #
# Cycle 15 — apply_policy is called exactly once, after every SPM write.          #
# --------------------------------------------------------------------------- #
def test_apply_policy_called_exactly_once_after_all_spm_writes():
    AR, UR, AS, TS, catalog = _repro()
    store = run_engine([_rule(UR, AS), _rule(AR, TS), _rule(UR, TS)], catalog=catalog)

    assert store.apply_policy_count == 1
    # both SPMs (agent + tool) were persisted before the single push
    assert {sid for sid, _ in store.service_writes} == {"github-agent", "github-tool"}


# --------------------------------------------------------------------------- #
# Cycle 16 — a service absent from the store (404) is seeded from the catalog     #
# and still persisted.                                                            #
# --------------------------------------------------------------------------- #
def test_absent_service_is_seeded_and_persisted():
    AR, UR, AS, TS, catalog = _repro()
    store = run_engine([_rule(UR, AS)], catalog=catalog)  # empty store -> 404 for both

    spm = store.data["github-agent"]
    assert spm.service_type == ServiceType.AGENT
    assert [r.id for r in spm.owned_roles] == ["r-agent-src"]  # seeded from catalog
    assert _pairs(spm.inbound_rules) == [("r-user-dev", "s-agent-inbound")]


# --------------------------------------------------------------------------- #
# Cycle 17 — a dependency failure is logged and RE-RAISED (not swallowed), so the  #
# caller (Controller) surfaces it as a real error instead of a silent 200 with     #
# nothing applied; nothing is pushed to the PDP.                                 #
# --------------------------------------------------------------------------- #
def test_dependency_exception_propagates(caplog):
    store = FakeStore()
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"KEYCLOAK_REALM": "test-realm"}))
        stack.enter_context(patch.object(Configuration, "get_services", side_effect=RuntimeError("boom")))
        stack.enter_context(patch("aiac.policy.computation.engine.get_service_policy",
                                  side_effect=store.get_service_policy))
        stack.enter_context(patch("aiac.policy.computation.engine.get_service_policies_by_role",
                                  side_effect=store.get_service_policies_by_role))
        stack.enter_context(patch("aiac.policy.computation.engine.apply_service_policy",
                                  side_effect=store.apply_service_policy))
        stack.enter_context(patch("aiac.policy.computation.engine.apply_policy",
                                  side_effect=store.apply_policy))
        from aiac.policy.computation.engine import compute_and_apply

        UR = _user_role("r-user", users=["u"])
        with pytest.raises(RuntimeError, match="boom"):
            compute_and_apply([_rule(UR, _scope("s-x", service_id="svc"))])
        assert store.apply_policy_count == 0
        assert "compute_and_apply failed" in caplog.text  # still logged before re-raising


# --------------------------------------------------------------------------- #
# Reconcile — drift GC (Handoff 10). Keycloak UUIDs churn on delete/recreate,   #
# so an append-only merge would grow stale edges beside their superseded         #
# generations (issue 6.3 / RC-A: SPM(github-agent) held 53 edges). On every      #
# compute the engine reconciles each TOUCHED SPM against the current             #
# get_services() catalog (no extra IdP read), dropping dangling edges. It        #
# removes only edges whose entity is gone, so live edges / order-independence    #
# are untouched.                                                                 #
# --------------------------------------------------------------------------- #
def test_reconcile_drops_retired_scope_edge():
    # A pre-fix ``*-aud`` scope no longer in the catalog is pruned on re-onboarding; the current
    # edge survives. Reconcile alone changes the SPM, so it is (re-)persisted.
    UR = _user_role("r-user-dev", "developer", users=["dev-user"])
    AS = _scope("s-agent-inbound", "agent-inbound", service_id="github-agent")
    aud = _scope("s-aud", "agent-team1-github-agent-aud", service_id="github-agent")
    catalog = [_agent("github-agent", scopes=[AS])]  # ``aud`` no longer exists
    initial = {
        "github-agent": _spm(
            "github-agent", owned_scopes=[AS], inbound=[_rule(UR, aud), _rule(UR, AS)]
        )
    }
    store = run_engine([_rule(UR, AS)], catalog=catalog, store_initial=initial)

    assert _pairs(store.data["github-agent"].inbound_rules) == [("r-user-dev", "s-agent-inbound")]


def test_reconcile_drops_churned_scope_uuid_same_name():
    # The scope was recreated with a fresh UUID (same name); the old-UUID edge is pruned.
    UR = _user_role("r-user-dev", "developer", users=["dev-user"])
    as_v1 = _scope("s-as-v1", "agent-inbound", service_id="github-agent")
    as_v2 = _scope("s-as-v2", "agent-inbound", service_id="github-agent")
    catalog = [_agent("github-agent", scopes=[as_v2])]  # only the current generation
    initial = {
        "github-agent": _spm(
            "github-agent", owned_scopes=[as_v1], inbound=[_rule(UR, as_v1)]
        )
    }
    store = run_engine([_rule(UR, as_v2)], catalog=catalog, store_initial=initial)

    assert _pairs(store.data["github-agent"].inbound_rules) == [("r-user-dev", "s-as-v2")]


def test_reconcile_collapses_churned_duplicate_user_role():
    # Two same-name/different-id ``developer`` edges on one scope (Keycloak delete+recreate). The
    # batch carries the current generation, so the old-generation edge is dropped; the derived APM's
    # subject gate then names only the current role.
    dev_old = _user_role("r-dev-v1", "developer", users=["dev-user"])
    dev_new = _user_role("r-dev-v2", "developer", users=["dev-user"])
    AS = _scope("s-agent-inbound", "agent-inbound", service_id="github-agent")
    catalog = [_agent("github-agent", scopes=[AS])]
    initial = {
        "github-agent": _spm(
            "github-agent", owned_scopes=[AS], inbound=[_rule(dev_old, AS), _rule(dev_new, AS)]
        )
    }
    store = run_engine([_rule(dev_new, AS)], catalog=catalog, store_initial=initial)

    assert _pairs(store.data["github-agent"].inbound_rules) == [("r-dev-v2", "s-agent-inbound")]
    apm = store.pushed_agent("github-agent")
    assert apm.subject_roles == {"dev-user": [dev_new]}


def test_reconcile_drops_retired_agent_role_self_reference():
    # An impossible focus-agent self-reference (an Agent-kind role the current builder can no longer
    # emit) references a role id absent from the catalog — pruned.
    UR = _user_role("r-user-dev", "developer", users=["dev-user"])
    AR = _agent_role("r-agent-src", "agent-source", owner="github-agent")  # current agent role
    selfref = _agent_role("r-selfref", "github-agent.agent", owner="github-agent")  # retired
    AS = _scope("s-agent-inbound", "agent-inbound", service_id="github-agent")
    catalog = [_agent("github-agent", roles=[AR], scopes=[AS])]  # r-selfref not present
    initial = {
        "github-agent": _spm(
            "github-agent", owned_scopes=[AS], inbound=[_rule(selfref, AS), _rule(UR, AS)]
        )
    }
    store = run_engine([_rule(UR, AS)], catalog=catalog, store_initial=initial)

    assert _pairs(store.data["github-agent"].inbound_rules) == [("r-user-dev", "s-agent-inbound")]


def test_reconcile_preserves_live_edges_and_is_idempotent():
    # Re-onboarding a service whose edges are all current prunes nothing (order-independence): the
    # canonical repro's full edge set survives a second identical compute.
    AR, UR, AS, TS, catalog = _repro()
    initial = {
        "github-agent": _spm("github-agent", owned_scopes=[AS], inbound=[_rule(UR, AS)]),
        "github-tool": _spm(
            "github-tool", type=ServiceType.TOOL, owned_scopes=[TS],
            inbound=[_rule(AR, TS), _rule(UR, TS)],
        ),
    }
    store = run_engine(
        [_rule(UR, AS), _rule(AR, TS), _rule(UR, TS)], catalog=catalog, store_initial=initial
    )

    assert _pairs(store.data["github-agent"].inbound_rules) == [("r-user-dev", "s-agent-inbound")]
    assert _pairs(store.data["github-tool"].inbound_rules) == sorted(
        [("r-agent-src", "s-tool-read"), ("r-user-dev", "s-tool-read")]
    )


def test_reconcile_skips_when_service_absent_from_catalog():
    # A transient catalog miss (owning service not returned by get_services()) must never wipe an
    # SPM — reconcile is skipped and the stale edge is left intact rather than dropped.
    UR = _user_role("r-user-dev", "developer", users=["dev-user"])
    orphan_scope = _scope("s-orphan", "orphan", service_id="orphan")
    initial = {
        "orphan": _spm("orphan", owned_scopes=[orphan_scope], inbound=[_rule(UR, orphan_scope)])
    }
    store = run_engine([_rule(UR, orphan_scope)], catalog=[], store_initial=initial)

    assert _pairs(store.data["orphan"].inbound_rules) == [("r-user-dev", "s-orphan")]


# --------------------------------------------------------------------------- #
# decommission (service offboard) — the onboard→offboard drift case (case 3).    #
# Reconcile's catalog-anchored GC skips a decommissioned service (absent from    #
# get_services()); decommission() is the authoritative teardown: delete SPM(X),  #
# purge X's outbound footprint from other SPMs, delete APM(X) if X is an agent,   #
# and re-derive every agent whose policy changed. Two-phase: onboard the repro    #
# into a shared store, then decommission against a catalog with X removed.        #
# --------------------------------------------------------------------------- #
def _onboard_repro(store):
    """Onboard the canonical repro into ``store`` (mutates it); return ``(AR, UR, AS, TS)``."""
    AR, UR, AS, TS, catalog = _repro()
    with engine_env(catalog, store) as compute_and_apply:
        compute_and_apply([_rule(UR, AS), _rule(AR, TS), _rule(UR, TS)])
    return AR, UR, AS, TS


def run_decommission(service_id, *, catalog, store) -> FakeStore:
    with engine_env(catalog, store):
        from aiac.policy.computation.engine import decommission

        decommission(service_id)
    return store


def test_decommission_tool_strands_no_edges_and_rederives_agent():
    # Onboard agent A (targets tool T), then offboard T. SPM(T) is deleted (with its user→T and
    # agent→T inbound edges); A is re-derived with its outbound to T dropped, its own inbound intact.
    store = FakeStore()
    _onboard_repro(store)
    # sanity: onboarding gave A an outbound edge to the tool.
    assert _pairs(store.pushed_agent("github-agent").outbound_rules) == [("r-agent-src", "s-tool-read")]

    # Phase 2: the tool is gone from the catalog (its Keycloak client was deleted).
    run_decommission("github-tool", catalog=[_agent("github-agent", scopes=[])], store=store)

    # SPM(T) deleted; a tool has no APM, so no PDP agent-delete.
    assert "github-tool" in store.service_deletes
    assert "github-tool" not in store.data
    assert store.agent_deletes == []

    # A re-derived: outbound to the tool is stranded (edge lived on SPM(T)); inbound UR→AS survives.
    apm = store.pushed_agent("github-agent")
    assert apm.outbound_rules == []
    assert apm.target_scopes == {}
    assert apm.outbound_subject_rules == []
    assert _pairs(apm.inbound_rules) == [("r-user-dev", "s-agent-inbound")]


def test_decommission_agent_deletes_apm_and_purges_outbound_footprint():
    # Offboard the agent A itself. SPM(A) is deleted, APM(A) is deleted from the PDP, and A's
    # outbound footprint (AR→TS stored on SPM(T)) is purged while the tool keeps its user grant.
    store = FakeStore()
    _onboard_repro(store)
    pushes_before = len(store.policy_pushes)

    # Phase 2: the agent is gone from the catalog.
    run_decommission("github-agent", catalog=[_tool("github-tool", scopes=[])], store=store)

    # SPM(A) deleted and APM(A) removed from the PDP.
    assert "github-agent" in store.service_deletes
    assert "github-agent" not in store.data
    assert store.agent_deletes == ["github-agent"]

    # A's outbound footprint purged from the tool; the tool keeps its user→TS grant.
    assert _pairs(store.data["github-tool"].inbound_rules) == [("r-user-dev", "s-tool-read")]

    # No APM re-derived for the deleted agent (nothing targeted it) — no new push.
    assert len(store.policy_pushes) == pushes_before
