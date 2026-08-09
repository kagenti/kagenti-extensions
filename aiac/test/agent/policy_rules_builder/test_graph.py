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
    PolicyRulesBuilderError,
    RoleSelection,
    ScopeSelection,
    _build_llm,
    build_role_rules,
    build_scope_rules,
)
from aiac.idp.configuration.models import Role, Scope
from aiac.policy.model.models import PolicyRule
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
