"""Policy Rules Builder graph: fetch -> propose -> precheck -> audit -> build.

Hides LangGraph behind two plain functions (build_role_rules / build_scope_rules)
that return list[PolicyRule]. The LLM is built lazily (never at import) and every
structured call is transport-retried via a call-time tenacity Retrying. On failure
the builder RAISES (policy-source failure, LLM failure after retries, audit-budget
exhaustion) -- never a silent []. An auditor-approved empty selection is a valid [].
"""

import logging
import os
from typing import Any, TypedDict, TypeVar, cast

from langchain_core.messages import BaseMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, SecretStr
from tenacity import Retrying, retry_if_exception, stop_after_attempt, wait_exponential

from aiac.idp.configuration.models import Role, Scope
from aiac.policy.model.models import PolicyRule
from aiac.shared.upstream import is_transient, max_retries

from .policy_source import get_policy_source
from .prompts import build_auditor_messages, build_proposer_messages

logger = logging.getLogger(__name__)
MAX_AUDIT_RETRIES = 3
_DEFAULT_LLM_REQUEST_TIMEOUT = 120.0


def _request_timeout() -> float:
    """Per-request LLM timeout (seconds) from ``LLM_REQUEST_TIMEOUT`` (default 120),
    tolerant of an unset or non-numeric value — a bad value must not crash the request,
    it falls back to the default (mirrors ``aiac.shared.upstream.max_retries``). Without
    it a stalled connection never raises and the whole ``/apply`` request wedges forever."""
    try:
        value = float(os.getenv("LLM_REQUEST_TIMEOUT", str(_DEFAULT_LLM_REQUEST_TIMEOUT)))
    except (TypeError, ValueError):
        return _DEFAULT_LLM_REQUEST_TIMEOUT
    return value if value > 0 else _DEFAULT_LLM_REQUEST_TIMEOUT


class _Selection(BaseModel):
    """Common proposer-output shape; the direction-specific names field is read
    by name (names_field) while reasoning is accessed directly."""

    reasoning: str


class RoleSelection(_Selection):
    granted_scope_names: list[str]


class ScopeSelection(_Selection):
    roles_with_access_names: list[str]


class AuditVerdict(BaseModel):
    approved: bool
    reason: str | None = None


class PolicyRulesBuilderError(RuntimeError): ...


class _PRBWorking(TypedDict):
    policy_text: str
    selected_names: list[str]
    reasoning: str
    approved: bool
    audit_feedback: str | None
    retry_count: int
    rules: list[PolicyRule]


class RoleRulesState(_PRBWorking):
    role: Role
    scopes: list[Scope]


class ScopeRulesState(_PRBWorking):
    roles: list[Role]
    scope: Scope


def _build_llm() -> ChatOpenAI:  # lazy -- NEVER called at import
    return ChatOpenAI(
        base_url=os.getenv("LLM_BASE_URL"),
        model=os.getenv("LLM_MODEL", ""),
        api_key=SecretStr(os.getenv("LLM_API_KEY", "")),
        temperature=0,
        # Fail fast on a stalled socket; retries are owned by _structured_call's tenacity
        # Retrying, so disable the client's own so attempts don't multiply.
        timeout=_request_timeout(),
        max_retries=0,
    )


T = TypeVar("T", bound=BaseModel)


def _structured_call(schema: type[T], messages: list[BaseMessage]) -> T:
    """THE seam. Behavior tests patch this. Transport-retries each .invoke() via call-time Retrying.

    Only transient failures (connection errors / timeouts / 5xx) are retried — a permanent
    failure (e.g. a bad request or a validation error) fails identically on every attempt, so
    it is surfaced immediately (consistent with ``aiac.shared.upstream``)."""
    runnable = _build_llm().with_structured_output(schema)
    retryer = Retrying(
        retry=retry_if_exception(is_transient),
        stop=stop_after_attempt(max_retries()),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        reraise=True,
    )
    return cast(T, retryer(runnable.invoke, messages))


# shared node helpers (typed against _PRBWorking; direction specifics passed as kwargs)
def _fetch(state: _PRBWorking) -> dict[str, Any]:
    return {"policy_text": get_policy_source().fetch()}


def _propose(
    state: _PRBWorking,
    *,
    focal: str,
    candidates: str,
    contract: str,
    schema: type[_Selection],
    names_field: str,
) -> dict[str, Any]:
    msgs = build_proposer_messages(state["policy_text"], focal, candidates, contract, state["audit_feedback"])
    sel = _structured_call(schema, msgs)
    return {"selected_names": list(getattr(sel, names_field)), "reasoning": sel.reasoning}


def _precheck(state: _PRBWorking, *, candidate_names: set[str]) -> dict[str, Any]:
    keep = [n for n in state["selected_names"] if n in candidate_names]
    dropped = [n for n in state["selected_names"] if n not in candidate_names]
    if dropped:
        logger.warning("PRB precheck dropped hallucinated names: %s", dropped)
    return {"selected_names": keep}


def _audit(state: _PRBWorking, *, focal: str, candidates: str) -> dict[str, Any]:
    verdict = _structured_call(
        AuditVerdict,
        build_auditor_messages(state["policy_text"], focal, candidates, state["selected_names"]),
    )
    if verdict.approved:
        return {"approved": True}
    if state["retry_count"] >= MAX_AUDIT_RETRIES:
        raise PolicyRulesBuilderError(f"Auditor rejected after {MAX_AUDIT_RETRIES} retries: {verdict.reason}")
    return {"approved": False, "audit_feedback": verdict.reason, "retry_count": state["retry_count"] + 1}


def _route(state: _PRBWorking) -> str:
    return "approved" if state["approved"] else "rejected"


def _role_focal(r: Role) -> str:
    return f"role name={r.name}: {r.description or ''}"


def _scope_focal(s: Scope) -> str:
    return f"scope name={s.name}: {s.description or ''}"


def _scope_cands(ss: list[Scope]) -> str:
    return "\n".join(_scope_focal(s) for s in ss)


def _role_cands(rs: list[Role]) -> str:
    return "\n".join(_role_focal(r) for r in rs)


_ROLE_CONTRACT = "Return granted_scope_names (subset of candidate scope names) + reasoning."
_SCOPE_CONTRACT = "Return roles_with_access_names (subset of candidate role names) + reasoning."


def _assemble(state_type: type, propose, precheck, audit, build):
    """Wire the shared fetch -> propose -> precheck -> audit -> build shape with the
    audit -> propose retry edge. Both directions differ only in their four closures."""
    g = StateGraph(state_type)
    g.add_node("fetch", _fetch)
    g.add_node("propose", propose)
    g.add_node("precheck", precheck)
    g.add_node("audit", audit)
    g.add_node("build", build)
    g.add_edge(START, "fetch")
    g.add_edge("fetch", "propose")
    g.add_edge("propose", "precheck")
    g.add_edge("precheck", "audit")
    g.add_conditional_edges("audit", _route, {"approved": "build", "rejected": "propose"})
    g.add_edge("build", END)
    return g.compile()


def build_role_graph():
    def propose(s: RoleRulesState) -> dict[str, Any]:
        return _propose(
            s,
            focal=_role_focal(s["role"]),
            candidates=_scope_cands(s["scopes"]),
            contract=_ROLE_CONTRACT,
            schema=RoleSelection,
            names_field="granted_scope_names",
        )

    def precheck(s: RoleRulesState) -> dict[str, Any]:
        return _precheck(s, candidate_names={sc.name for sc in s["scopes"]})

    def audit(s: RoleRulesState) -> dict[str, Any]:
        return _audit(s, focal=_role_focal(s["role"]), candidates=_scope_cands(s["scopes"]))

    def build(s: RoleRulesState) -> dict[str, Any]:
        granted = set(s["selected_names"])
        return {"rules": [PolicyRule(role=s["role"], scope=sc) for sc in s["scopes"] if sc.name in granted]}

    return _assemble(RoleRulesState, propose, precheck, audit, build)


def build_scope_graph():
    def propose(s: ScopeRulesState) -> dict[str, Any]:
        return _propose(
            s,
            focal=_scope_focal(s["scope"]),
            candidates=_role_cands(s["roles"]),
            contract=_SCOPE_CONTRACT,
            schema=ScopeSelection,
            names_field="roles_with_access_names",
        )

    def precheck(s: ScopeRulesState) -> dict[str, Any]:
        return _precheck(s, candidate_names={r.name for r in s["roles"]})

    def audit(s: ScopeRulesState) -> dict[str, Any]:
        return _audit(s, focal=_scope_focal(s["scope"]), candidates=_role_cands(s["roles"]))

    def build(s: ScopeRulesState) -> dict[str, Any]:
        granted = set(s["selected_names"])
        return {"rules": [PolicyRule(role=r, scope=s["scope"]) for r in s["roles"] if r.name in granted]}

    return _assemble(ScopeRulesState, propose, precheck, audit, build)


ROLE_GRAPH = build_role_graph()  # module-level compile is safe (never builds the LLM)
SCOPE_GRAPH = build_scope_graph()


def build_role_rules(role: Role, scopes: list[Scope]) -> list[PolicyRule]:
    state: RoleRulesState = {
        "role": role,
        "scopes": scopes,
        "policy_text": "",
        "selected_names": [],
        "reasoning": "",
        "approved": False,
        "audit_feedback": None,
        "retry_count": 0,
        "rules": [],
    }
    return ROLE_GRAPH.invoke(state)["rules"]


def build_scope_rules(roles: list[Role], scope: Scope) -> list[PolicyRule]:
    state: ScopeRulesState = {
        "roles": roles,
        "scope": scope,
        "policy_text": "",
        "selected_names": [],
        "reasoning": "",
        "approved": False,
        "audit_feedback": None,
        "retry_count": 0,
        "rules": [],
    }
    return SCOPE_GRAPH.invoke(state)["rules"]
