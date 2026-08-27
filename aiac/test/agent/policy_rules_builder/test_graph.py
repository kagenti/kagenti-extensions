"""Unit tests for aiac.agent.policy_rules_builder.graph.

The LLM is mocked at the module's structured-call boundary
(aiac.agent.policy_rules_builder.graph._structured_call) so no live endpoint is
touched; the policy source is stubbed at graph.get_policy_source. Transport
retries (slice 9) patch graph._build_llm + time.sleep instead.
"""

from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest
from openai import APIConnectionError, APITimeoutError

from aiac.agent.policy_rules_builder.graph import (
    AuditVerdict,
    Contradiction,
    PolicyContradictionError,
    PolicyRulesBuilderError,
    RoleSelection,
    ScopeSelection,
    _build_llm,
    _build_llm,
    build_role_rules,
    build_scope_rules,
)
from aiac.idp.configuration.models import Role, Scope
from aiac.policy.model.models import PolicyRule, RuleEffect
from aiac.shared.upstream import is_transient


# --------------------------------------------------------------------------- #
# builders (mirror test/policy/computation/test_engine.py)                    #
# --------------------------------------------------------------------------- #
def _role(id="r-edit", name="editor", composite=False, children=None) -> Role:
    return Role(id=id, name=name, composite=composite, childRoles=children or [])


def _scope(id="s-write", name="write") -> Scope:
    return Scope(id=id, name=name)


class _Source:
    """Stub PolicySource whose fetch() returns a fixed policy string."""

    def __init__(self, text="POLICY"):
        self.text = text

    def fetch(self) -> str:
        return self.text


# --------------------------------------------------------------------------- #
# Slice 1 — tracer: build_role_rules happy path. The proposer grants one       #
# candidate scope by name and the auditor approves; a single PolicyRule for     #
# that (role, scope) pair comes back.                                          #
# --------------------------------------------------------------------------- #
def test_build_role_rules_happy_path():
    role = _role()
    write = _scope("s-write", "write")

    with ExitStack() as stack:
        stack.enter_context(patch("aiac.agent.policy_rules_builder.graph.get_policy_source", return_value=_Source()))
        stack.enter_context(
            patch(
                "aiac.agent.policy_rules_builder.graph._structured_call",
                side_effect=[
                    RoleSelection(granted_scope_names=["write"], reasoning="r"),
                    AuditVerdict(approved=True),
                ],
            )
        )
        rules = build_role_rules(role, [write])

    assert rules == [PolicyRule(role=role, scope=write)]


# --------------------------------------------------------------------------- #
# Slice 2 — build_scope_rules happy path (mirror). Scope is focal, roles are    #
# the candidates; the proposer names one role, the auditor approves.           #
# --------------------------------------------------------------------------- #
def test_build_scope_rules_happy_path():
    editor = _role("r-edit", "editor")
    scope = _scope("s-write", "write")

    with ExitStack() as stack:
        stack.enter_context(patch("aiac.agent.policy_rules_builder.graph.get_policy_source", return_value=_Source()))
        stack.enter_context(
            patch(
                "aiac.agent.policy_rules_builder.graph._structured_call",
                side_effect=[
                    ScopeSelection(roles_with_access_names=["editor"], reasoning="r"),
                    AuditVerdict(approved=True),
                ],
            )
        )
        rules = build_scope_rules([editor], scope)

    assert rules == [PolicyRule(role=editor, scope=scope)]


# --------------------------------------------------------------------------- #
# Slice 3 — precheck drops proposer names not in the candidate set BEFORE the   #
# auditor sees them: the proposer hallucinates "ghost", so the auditor audits   #
# only the real "write" selection and just the write rule is built.            #
# --------------------------------------------------------------------------- #
def test_precheck_drops_hallucinated_names():
    role = _role()
    write = _scope("s-write", "write")

    with ExitStack() as stack:
        stack.enter_context(patch("aiac.agent.policy_rules_builder.graph.get_policy_source", return_value=_Source()))
        sc = stack.enter_context(
            patch(
                "aiac.agent.policy_rules_builder.graph._structured_call",
                side_effect=[
                    RoleSelection(granted_scope_names=["write", "ghost"], reasoning="r"),
                    AuditVerdict(approved=True),
                ],
            )
        )
        rules = build_role_rules(role, [write])

    assert rules == [PolicyRule(role=role, scope=write)]
    # The auditor (2nd structured call) must see the cleaned selection, not "ghost".
    auditor_msg = sc.call_args_list[1].args[1][1].content
    assert "write" in auditor_msg and "ghost" not in auditor_msg


# --------------------------------------------------------------------------- #
# Slice 4 — an auditor-approved empty selection is a valid [] (deny-by-default) #
# and NOT an error. The proposer grants nothing; the auditor approves.         #
# --------------------------------------------------------------------------- #
def test_approved_empty_selection_returns_empty():
    role = _role()
    write = _scope("s-write", "write")

    with ExitStack() as stack:
        stack.enter_context(patch("aiac.agent.policy_rules_builder.graph.get_policy_source", return_value=_Source()))
        stack.enter_context(
            patch(
                "aiac.agent.policy_rules_builder.graph._structured_call",
                side_effect=[
                    RoleSelection(granted_scope_names=[], reasoning="policy is silent"),
                    AuditVerdict(approved=True),
                ],
            )
        )
        rules = build_role_rules(role, [write])

    assert rules == []


# --------------------------------------------------------------------------- #
# Slice 5 — auditor rejects the first proposal, the builder re-proposes carrying #
# the rejection reason, then the auditor approves. Rules come back AND the 2nd   #
# proposer call was threaded the prior reason.                                  #
# --------------------------------------------------------------------------- #
def test_auditor_reject_then_approve_threads_feedback():
    role = _role()
    write = _scope("s-write", "write")

    with ExitStack() as stack:
        stack.enter_context(patch("aiac.agent.policy_rules_builder.graph.get_policy_source", return_value=_Source()))
        sc = stack.enter_context(
            patch(
                "aiac.agent.policy_rules_builder.graph._structured_call",
                side_effect=[
                    RoleSelection(granted_scope_names=["write"], reasoning="r"),
                    AuditVerdict(approved=False, reason="scope X unsupported"),
                    RoleSelection(granted_scope_names=["write"], reasoning="r2"),
                    AuditVerdict(approved=True),
                ],
            )
        )
        rules = build_role_rules(role, [write])

    assert rules == [PolicyRule(role=role, scope=write)]
    # 3rd structured call is the re-proposal; its user message must carry the reason.
    reproposal_msg = sc.call_args_list[2].args[1][1].content
    assert "scope X unsupported" in reproposal_msg


# --------------------------------------------------------------------------- #
# Slice 6 — a persistently-rejecting auditor exhausts the audit budget and the  #
# builder RAISES PolicyRulesBuilderError rather than returning a silent [].     #
# --------------------------------------------------------------------------- #
def test_auditor_rejects_past_budget_raises():
    role = _role()
    write = _scope("s-write", "write")

    def se(schema, messages):
        if schema is AuditVerdict:
            return AuditVerdict(approved=False, reason="never ok")
        return RoleSelection(granted_scope_names=["write"], reasoning="r")

    with ExitStack() as stack:
        stack.enter_context(patch("aiac.agent.policy_rules_builder.graph.get_policy_source", return_value=_Source()))
        stack.enter_context(patch("aiac.agent.policy_rules_builder.graph._structured_call", side_effect=se))
        with pytest.raises(PolicyRulesBuilderError):
            build_role_rules(role, [write])


# --------------------------------------------------------------------------- #
# Slice 9 — a persistently-unavailable LLM is transport-retried UPSTREAM_MAX_    #
# RETRIES times, then the original transport error propagates (never swallowed). #
# time.sleep is patched so tenacity's backoff waits are skipped.               #
# --------------------------------------------------------------------------- #
def test_llm_unavailable_raises_after_upstream_max_retries(monkeypatch):
    monkeypatch.setenv("UPSTREAM_MAX_RETRIES", "2")

    invoke = MagicMock(side_effect=ConnectionError("down"))
    runnable = MagicMock()
    runnable.invoke = invoke
    llm = MagicMock()
    llm.with_structured_output.return_value = runnable

    with ExitStack() as stack:
        stack.enter_context(patch("aiac.agent.policy_rules_builder.graph.get_policy_source", return_value=_Source()))
        stack.enter_context(patch("aiac.agent.policy_rules_builder.graph._build_llm", return_value=llm))
        stack.enter_context(patch("time.sleep"))  # NOT tenacity.nap.sleep (ineffective)
        with pytest.raises(ConnectionError):
            build_role_rules(_role(), [_scope("s-write", "write")])

    assert invoke.call_count == 2


# --------------------------------------------------------------------------- #
# Slice 10 — request-timeout robustness. The openai/langchain_openai timeout    #
# and dropped-connection exceptions must be classified transient so the         #
# _structured_call retry wrapper self-heals instead of letting /apply hang.     #
# --------------------------------------------------------------------------- #
def _openai_request():
    import httpx

    return httpx.Request("POST", "https://llm.example/v1/chat/completions")


@pytest.mark.parametrize(
    "exc",
    [
        APITimeoutError(request=_openai_request()),
        APIConnectionError(request=_openai_request()),
    ],
)
def test_openai_timeout_and_connection_errors_are_transient(exc):
    assert is_transient(exc) is True


def test_llm_timeout_is_retried_then_reraised(monkeypatch):
    """A never-returning LLM that raises APITimeoutError is retried UPSTREAM_MAX_RETRIES
    times and then surfaces — proving the timeout path self-heals rather than wedging."""
    monkeypatch.setenv("UPSTREAM_MAX_RETRIES", "2")

    invoke = MagicMock(side_effect=APITimeoutError(request=_openai_request()))
    runnable = MagicMock()
    runnable.invoke = invoke
    llm = MagicMock()
    llm.with_structured_output.return_value = runnable

    with ExitStack() as stack:
        stack.enter_context(patch("aiac.agent.policy_rules_builder.graph.get_policy_source", return_value=_Source()))
        stack.enter_context(patch("aiac.agent.policy_rules_builder.graph._build_llm", return_value=llm))
        stack.enter_context(patch("time.sleep"))
        with pytest.raises(APITimeoutError):
            build_role_rules(_role(), [_scope("s-write", "write")])

    assert invoke.call_count == 2


# --------------------------------------------------------------------------- #
# Slice 11 — _build_llm sources a non-None request timeout from                 #
# LLM_REQUEST_TIMEOUT and disables the client's own retries (tenacity owns      #
# retry). Without a timeout a stalled socket never raises.                      #
# --------------------------------------------------------------------------- #
def test_build_llm_sets_request_timeout_from_env(monkeypatch):
    monkeypatch.setenv("LLM_REQUEST_TIMEOUT", "45")
    monkeypatch.setenv("LLM_MODEL", "test-model")

    with patch("aiac.agent.policy_rules_builder.graph.ChatOpenAI") as mk:
        _build_llm()

    kwargs = mk.call_args.kwargs
    assert kwargs["timeout"] == 45
    assert kwargs["max_retries"] == 0


def test_build_llm_defaults_timeout_on_bad_env(monkeypatch):
    monkeypatch.setenv("LLM_REQUEST_TIMEOUT", "not-a-number")
    monkeypatch.setenv("LLM_MODEL", "test-model")

    with patch("aiac.agent.policy_rules_builder.graph.ChatOpenAI") as mk:
        _build_llm()

    assert mk.call_args.kwargs["timeout"] == 120


# =========================================================================== #
# #123 — deny extraction from natural-language policy text.                    #
# The proposer now returns explicit prohibitions (denied_* name lists) and an  #
# exclusivity flag alongside its grants; build emits ALLOW rules for grants    #
# and DENY rules for prohibitions (+ the derived exclusivity complement),      #
# allows-first then denies, each in candidate order. All cases drive the       #
# existing _structured_call seam — proposer + auditor turns interleaved.       #
# =========================================================================== #


# --------------------------------------------------------------------------- #
# Slice A (tracer) — direct prohibition -> DENY, role direction. "developers    #
# may read but must not touch issues": the proposer grants `read` and denies    #
# `issues`; the auditor approves; an ALLOW(read) + DENY(issues) pair comes back.#
# --------------------------------------------------------------------------- #
def test_direct_prohibition_yields_deny_role_direction():
    role = _role("r-dev", "developer")
    read = _scope("s-read", "read")
    issues = _scope("s-issues", "issues")

    with ExitStack() as stack:
        stack.enter_context(patch("aiac.agent.policy_rules_builder.graph.get_policy_source", return_value=_Source()))
        stack.enter_context(
            patch(
                "aiac.agent.policy_rules_builder.graph._structured_call",
                side_effect=[
                    RoleSelection(
                        granted_scope_names=["read"],
                        denied_scope_names=["issues"],
                        grant_is_exclusive=False,
                        reasoning="may read but must not touch issues",
                    ),
                    AuditVerdict(approved=True),
                ],
            )
        )
        rules = build_role_rules(role, [read, issues])

    assert rules == [
        PolicyRule(role=role, scope=read, effect=RuleEffect.ALLOW),
        PolicyRule(role=role, scope=issues, effect=RuleEffect.DENY),
    ]


# --------------------------------------------------------------------------- #
# Slice B — symmetric direct prohibition -> DENY, scope direction. The scope is #
# focal, roles are candidates: one role is granted access, another is denied.   #
# --------------------------------------------------------------------------- #
def test_direct_prohibition_yields_deny_scope_direction():
    scope = _scope("s-audit", "audit-log")
    security = _role("r-sec", "security")
    intern = _role("r-int", "intern")

    with ExitStack() as stack:
        stack.enter_context(patch("aiac.agent.policy_rules_builder.graph.get_policy_source", return_value=_Source()))
        stack.enter_context(
            patch(
                "aiac.agent.policy_rules_builder.graph._structured_call",
                side_effect=[
                    ScopeSelection(
                        roles_with_access_names=["security"],
                        roles_denied_access_names=["intern"],
                        access_is_exclusive=False,
                        reasoning="security may reach the audit log; interns must not",
                    ),
                    AuditVerdict(approved=True),
                ],
            )
        )
        rules = build_scope_rules([security, intern], scope)

    assert rules == [
        PolicyRule(role=security, scope=scope, effect=RuleEffect.ALLOW),
        PolicyRule(role=intern, scope=scope, effect=RuleEffect.DENY),
    ]


# --------------------------------------------------------------------------- #
# Slice C — exclusivity ("developers can ONLY access source") -> the derived    #
# complement. grant_is_exclusive=True with granted=[source] over {source,       #
# issues,deploy} yields ALLOW(source) + DENY(issues) + DENY(deploy). The        #
# complement is DERIVED from the candidate set, not enumerated by the proposer  #
# (denied_scope_names is empty).                                               #
# --------------------------------------------------------------------------- #
def test_exclusivity_derives_complement_role_direction():
    role = _role("r-dev", "developer")
    source = _scope("s-src", "source")
    issues = _scope("s-iss", "issues")
    deploy = _scope("s-dep", "deploy")

    with ExitStack() as stack:
        stack.enter_context(patch("aiac.agent.policy_rules_builder.graph.get_policy_source", return_value=_Source()))
        stack.enter_context(
            patch(
                "aiac.agent.policy_rules_builder.graph._structured_call",
                side_effect=[
                    RoleSelection(
                        granted_scope_names=["source"],
                        denied_scope_names=[],
                        grant_is_exclusive=True,
                        reasoning="developers can only access source",
                    ),
                    AuditVerdict(approved=True),
                ],
            )
        )
        rules = build_role_rules(role, [source, issues, deploy])

    assert rules == [
        PolicyRule(role=role, scope=source, effect=RuleEffect.ALLOW),
        PolicyRule(role=role, scope=issues, effect=RuleEffect.DENY),
        PolicyRule(role=role, scope=deploy, effect=RuleEffect.DENY),
    ]


# --------------------------------------------------------------------------- #
# Slice D — exclusivity symmetric, scope direction ("ONLY developers may access #
# source"). access_is_exclusive=True with granted=[developer] over the role     #
# candidate set denies every OTHER candidate role for the focal scope.         #
# --------------------------------------------------------------------------- #
def test_exclusivity_derives_complement_scope_direction():
    scope = _scope("s-src", "source")
    dev = _role("r-dev", "developer")
    tester = _role("r-tst", "tester")
    ops = _role("r-ops", "ops")

    with ExitStack() as stack:
        stack.enter_context(patch("aiac.agent.policy_rules_builder.graph.get_policy_source", return_value=_Source()))
        stack.enter_context(
            patch(
                "aiac.agent.policy_rules_builder.graph._structured_call",
                side_effect=[
                    ScopeSelection(
                        roles_with_access_names=["developer"],
                        roles_denied_access_names=[],
                        access_is_exclusive=True,
                        reasoning="only developers may access source",
                    ),
                    AuditVerdict(approved=True),
                ],
            )
        )
        rules = build_scope_rules([dev, tester, ops], scope)

    assert rules == [
        PolicyRule(role=dev, scope=scope, effect=RuleEffect.ALLOW),
        PolicyRule(role=tester, scope=scope, effect=RuleEffect.DENY),
        PolicyRule(role=ops, scope=scope, effect=RuleEffect.DENY),
    ]


# --------------------------------------------------------------------------- #
# Slice E — a NON-exclusive grant imposes nothing on the complement. "developers #
# may access source" (grant_is_exclusive=False, no explicit deny) grants source  #
# and leaves issues a silent non-grant -- no DENY(issues).                      #
# --------------------------------------------------------------------------- #
def test_non_exclusive_grant_imposes_no_complement_deny():
    role = _role("r-dev", "developer")
    source = _scope("s-src", "source")
    issues = _scope("s-iss", "issues")

    with ExitStack() as stack:
        stack.enter_context(patch("aiac.agent.policy_rules_builder.graph.get_policy_source", return_value=_Source()))
        stack.enter_context(
            patch(
                "aiac.agent.policy_rules_builder.graph._structured_call",
                side_effect=[
                    RoleSelection(
                        granted_scope_names=["source"],
                        denied_scope_names=[],
                        grant_is_exclusive=False,
                        reasoning="developers may access source",
                    ),
                    AuditVerdict(approved=True),
                ],
            )
        )
        rules = build_role_rules(role, [source, issues])

    assert rules == [PolicyRule(role=role, scope=source, effect=RuleEffect.ALLOW)]


# --------------------------------------------------------------------------- #
# Slice F — a genuine grant/deny overlap on the same candidate (a coarse scope   #
# "may read issues but must not modify them", where `issues` covers read+write)  #
# is a contradiction. precheck flags issues in BOTH lists; the auditor           #
# adjudicates it genuine, so the builder RAISES PolicyContradictionError         #
# carrying the focal entity and the contradiction (with its description),        #
# fail-closed -- no rule set is returned.                                       #
# --------------------------------------------------------------------------- #
def test_genuine_overlap_raises_policy_contradiction_error():
    role = _role("r-dev", "developer")
    issues = _scope("s-iss", "issues")

    with ExitStack() as stack:
        stack.enter_context(patch("aiac.agent.policy_rules_builder.graph.get_policy_source", return_value=_Source()))
        stack.enter_context(
            patch(
                "aiac.agent.policy_rules_builder.graph._structured_call",
                side_effect=[
                    RoleSelection(
                        granted_scope_names=["issues"],
                        denied_scope_names=["issues"],
                        grant_is_exclusive=False,
                        reasoning="may read issues but must not modify them",
                    ),
                    AuditVerdict(
                        approved=False,
                        contradictions=[
                            Contradiction(
                                candidate_name="issues",
                                description="coarse-scope granularity mismatch: issues covers read and write",
                            )
                        ],
                    ),
                ],
            )
        )
        with pytest.raises(PolicyContradictionError) as exc:
            build_role_rules(role, [issues])

    # The raise carries the focal identity (its name appears) and all genuine contradictions,
    # each with its description -- the report IS the raise; no rule set comes back.
    assert role.name in exc.value.focal
    assert [c.candidate_name for c in exc.value.contradictions] == ["issues"]
    assert "coarse-scope" in exc.value.contradictions[0].description


# --------------------------------------------------------------------------- #
# Slice G — multiple genuine contradictions are reported in a SINGLE raise, so   #
# the author can fix them all in one pass (not discover them one at a time).    #
# --------------------------------------------------------------------------- #
def test_multiple_contradictions_reported_in_one_raise():
    role = _role("r-dev", "developer")
    issues = _scope("s-iss", "issues")
    deploy = _scope("s-dep", "deploy")

    with ExitStack() as stack:
        stack.enter_context(patch("aiac.agent.policy_rules_builder.graph.get_policy_source", return_value=_Source()))
        stack.enter_context(
            patch(
                "aiac.agent.policy_rules_builder.graph._structured_call",
                side_effect=[
                    RoleSelection(
                        granted_scope_names=["issues", "deploy"],
                        denied_scope_names=["issues", "deploy"],
                        grant_is_exclusive=False,
                        reasoning="both coarse scopes are partly permitted and partly forbidden",
                    ),
                    AuditVerdict(
                        approved=False,
                        contradictions=[
                            Contradiction(candidate_name="issues", description="direct policy conflict"),
                            Contradiction(candidate_name="deploy", description="coarse-scope granularity mismatch"),
                        ],
                    ),
                ],
            )
        )
        with pytest.raises(PolicyContradictionError) as exc:
            build_role_rules(role, [issues, deploy])

    assert {c.candidate_name for c in exc.value.contradictions} == {"issues", "deploy"}


# --------------------------------------------------------------------------- #
# Slice H — a generation-error overlap is NOT a policy finding. The auditor      #
# rejects the first proposal with contradictions=[] (ordinary rejection); the    #
# builder threads the reason back, re-proposes cleanly, and the auditor          #
# approves. Rules come back, no PolicyContradictionError is raised.             #
# --------------------------------------------------------------------------- #
def test_generation_error_overlap_retries_then_approves():
    role = _role("r-dev", "developer")
    issues = _scope("s-iss", "issues")

    with ExitStack() as stack:
        stack.enter_context(patch("aiac.agent.policy_rules_builder.graph.get_policy_source", return_value=_Source()))
        sc = stack.enter_context(
            patch(
                "aiac.agent.policy_rules_builder.graph._structured_call",
                side_effect=[
                    RoleSelection(
                        granted_scope_names=["issues"],
                        denied_scope_names=["issues"],
                        grant_is_exclusive=False,
                        reasoning="accidentally listed issues in both",
                    ),
                    AuditVerdict(approved=False, reason="you listed issues as both granted and denied; pick one"),
                    RoleSelection(
                        granted_scope_names=["issues"],
                        denied_scope_names=[],
                        grant_is_exclusive=False,
                        reasoning="issues is granted only",
                    ),
                    AuditVerdict(approved=True),
                ],
            )
        )
        rules = build_role_rules(role, [issues])

    assert rules == [PolicyRule(role=role, scope=issues, effect=RuleEffect.ALLOW)]
    # The re-proposal (3rd structured call) must carry the auditor's rejection reason.
    reproposal_msg = sc.call_args_list[2].args[1][1].content
    assert "pick one" in reproposal_msg


# --------------------------------------------------------------------------- #
# Slice I — an all-deny result (a prohibition with no current grant) is a valid, #
# first-class output, NOT collapsed to []. It blocks a future broad grant under  #
# deny-overrides.                                                               #
# --------------------------------------------------------------------------- #
def test_all_deny_result_is_first_class():
    role = _role("r-dev", "developer")
    issues = _scope("s-iss", "issues")

    with ExitStack() as stack:
        stack.enter_context(patch("aiac.agent.policy_rules_builder.graph.get_policy_source", return_value=_Source()))
        stack.enter_context(
            patch(
                "aiac.agent.policy_rules_builder.graph._structured_call",
                side_effect=[
                    RoleSelection(
                        granted_scope_names=[],
                        denied_scope_names=["issues"],
                        grant_is_exclusive=False,
                        reasoning="developers must never touch issues",
                    ),
                    AuditVerdict(approved=True),
                ],
            )
        )
        rules = build_role_rules(role, [issues])

    assert rules == [PolicyRule(role=role, scope=issues, effect=RuleEffect.DENY)]


# --------------------------------------------------------------------------- #
# Slice J — precheck drops a hallucinated DENIED name before the auditor sees it #
# (symmetric with the existing granted-name hallucination-drop slice). "ghost"   #
# is not a candidate, so the auditor audits only the real "issues" prohibition. #
# --------------------------------------------------------------------------- #
def test_precheck_drops_hallucinated_denied_name():
    role = _role("r-dev", "developer")
    issues = _scope("s-iss", "issues")

    with ExitStack() as stack:
        stack.enter_context(patch("aiac.agent.policy_rules_builder.graph.get_policy_source", return_value=_Source()))
        sc = stack.enter_context(
            patch(
                "aiac.agent.policy_rules_builder.graph._structured_call",
                side_effect=[
                    RoleSelection(
                        granted_scope_names=[],
                        denied_scope_names=["issues", "ghost"],
                        grant_is_exclusive=False,
                        reasoning="must not touch issues",
                    ),
                    AuditVerdict(approved=True),
                ],
            )
        )
        rules = build_role_rules(role, [issues])

    assert rules == [PolicyRule(role=role, scope=issues, effect=RuleEffect.DENY)]
    auditor_msg = sc.call_args_list[1].args[1][1].content
    assert "issues" in auditor_msg and "ghost" not in auditor_msg


# --------------------------------------------------------------------------- #
# Slice K — a single call mixing grants and prohibitions returns BOTH, ordered   #
# deterministically: all ALLOWs first, then all DENYs, each in candidate order   #
# (stable + diffable across runs).                                              #
# --------------------------------------------------------------------------- #
def test_mixed_allow_and_deny_ordered_allows_then_denies_candidate_order():
    role = _role("r-dev", "developer")
    source = _scope("s-src", "source")
    issues = _scope("s-iss", "issues")
    deploy = _scope("s-dep", "deploy")
    audit = _scope("s-aud", "audit")

    with ExitStack() as stack:
        stack.enter_context(patch("aiac.agent.policy_rules_builder.graph.get_policy_source", return_value=_Source()))
        stack.enter_context(
            patch(
                "aiac.agent.policy_rules_builder.graph._structured_call",
                side_effect=[
                    RoleSelection(
                        granted_scope_names=["source", "deploy"],
                        denied_scope_names=["issues", "audit"],
                        grant_is_exclusive=False,
                        reasoning="may access source and deploy; must not touch issues or audit",
                    ),
                    AuditVerdict(approved=True),
                ],
            )
        )
        # Candidate order: source, issues, deploy, audit.
        rules = build_role_rules(role, [source, issues, deploy, audit])

    assert rules == [
        PolicyRule(role=role, scope=source, effect=RuleEffect.ALLOW),
        PolicyRule(role=role, scope=deploy, effect=RuleEffect.ALLOW),
        PolicyRule(role=role, scope=issues, effect=RuleEffect.DENY),
        PolicyRule(role=role, scope=audit, effect=RuleEffect.DENY),
    ]


# --------------------------------------------------------------------------- #
# Slice L — prompt content. (a) The proposer AND the auditor are told the        #
# deny/exclusivity contract (a one-sided rule would let them diverge): explicit  #
# prohibitions -> deny, and restrictive "only" closes the set. (b) The POLICY    #
# block labels the baseline as grants-only and the scenario separately, so       #
# deny/exclusivity binds to the scenario layer only. Asserted on the captured    #
# message content at the _structured_call seam.                                 #
# --------------------------------------------------------------------------- #
def _capture_first_two_messages():
    """Run one happy build_role_rules and return (proposer_msgs, auditor_msgs) as captured at the
    seam. Each is [SystemMessage, HumanMessage]."""
    role = _role("r-dev", "developer")
    write = _scope("s-write", "write")
    with ExitStack() as stack:
        stack.enter_context(
            patch("aiac.agent.policy_rules_builder.graph.get_policy_source", return_value=_Source("SCEN-TEXT"))
        )
        sc = stack.enter_context(
            patch(
                "aiac.agent.policy_rules_builder.graph._structured_call",
                side_effect=[
                    RoleSelection(granted_scope_names=["write"], reasoning="r"),
                    AuditVerdict(approved=True),
                ],
            )
        )
        build_role_rules(role, [write])
    return sc.call_args_list[0].args[1], sc.call_args_list[1].args[1]


def test_proposer_and_auditor_share_deny_and_exclusivity_contract():
    proposer_msgs, auditor_msgs = _capture_first_two_messages()
    for msgs in (proposer_msgs, auditor_msgs):
        system = msgs[0].content.lower()
        assert "prohibition" in system or "must not" in system  # explicit-prohibition -> deny
        assert "only" in system and "exclusiv" in system  # restrictive "only" closes the set


def test_policy_block_labels_baseline_grants_only_and_scenario():
    proposer_msgs, _ = _capture_first_two_messages()
    human = proposer_msgs[1].content
    assert "BASELINE POLICY" in human and "grants only" in human
    assert "SCENARIO POLICY" in human
    # The scenario text sits under the SCENARIO label, after the baseline.
    assert human.index("BASELINE POLICY") < human.index("SCENARIO POLICY") < human.index("SCEN-TEXT")
