"""Read-only policy Conflict-Check diagnostic engine (feature #154, task #157).

A PARALLEL diagnostic graph that sits NEXT TO the live Policy Rules Builder graph in
``graph.py`` and reuses its proposer / precheck / auditor machinery UNCHANGED, but differs
in three ways (design decisions D8/D11/D14 in
``docs/specs/components/aiac-agent/policy-conflict-check.md``):

  (a) START **seeds ``policy_text`` from the input state** -- it does NOT read
      ``AIAC_POLICY_FILE`` via ``get_policy_source()`` the way the live ``_fetch`` does. The
      candidate policy prose is supplied directly by the caller.
  (b) The audit node **RECORDS** genuine contradictions into state instead of RAISING
      ``PolicyContradictionError`` (the live ``_audit`` raises). On retry-budget exhaustion it
      marks the focal entity ``unevaluated`` -- again, never raising.
  (c) A terminal ``explain`` node (D11) CLASSIFIES the conflict ``kind`` and extracts verbatim,
      substring-validated quotes for each recorded (role, scope) contradiction. There is NO
      ``build`` node -- the diagnostic emits a ``ConflictReport`` fragment, not ``PolicyRule``s.

The live ``graph.py`` (``_audit`` raise, ``_fetch`` START, ``build_*`` entry points) is left
BYTE-FOR-BYTE UNCHANGED; this module imports the reusable pieces from it. Every LLM turn --
propose, audit, explain -- flows through the SAME ``graph._structured_call`` seam, so a single
``side_effect`` patch on ``aiac.agent.policy_rules_builder.graph._structured_call`` drives all
three in order (mirrors ``test_graph.py``).

This engine runs ONE focal entity end-to-end (one role-focal run or one scope-focal run). The
survey orchestrator (#158) loops it over every focal entity in a resolved ``FocalEntitySet`` and
assembles the top-level ``ConflictReport`` from the accumulated ``conflicts`` + ``unevaluated``.

Public per-entity entry points (what #158 calls):

    run_role_diagnostic(policy_text, role, scopes, *, focal_entities=None)  -> DiagnosticResult
    run_scope_diagnostic(policy_text, roles, scope, *, focal_entities=None) -> DiagnosticResult

each returning ``DiagnosticResult(conflicts=[...], unevaluated=[...])``. Per the resolver's
fan-out (#155): a scope-focal run is ``candidate_roles`` against one ``own_scope``; a role-focal
run (AGENT services only) is one ``own_role`` against ``other_scopes``.
"""

from typing import Any, NamedTuple, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel

from aiac.agent.shared.focal_entities import FocalEntitySet
from aiac.idp.configuration.models import Role, Scope

from . import graph as _graph
from .diagnostic_models import (
    Conflict,
    ConflictKind,
    EntityRef,
    FocalRef,
    FocalType,
    Unevaluated,
    UnevaluatedReason,
)
from .graph import (
    MAX_AUDIT_RETRIES,
    AuditVerdict,
    RoleSelection,
    ScopeSelection,
    _precheck,
    _propose,
    _PRBWorking,
    _role_cands,
    _role_focal,
    _scope_cands,
    _scope_focal,
    _ROLE_CONTRACT,
    _ROLE_DIRECTION,
    _SCOPE_CONTRACT,
    _SCOPE_DIRECTION,
)
from .prompts import build_auditor_messages, build_explain_messages


# --------------------------------------------------------------------------- #
# Explain-node output schema (driven through graph._structured_call).          #
# --------------------------------------------------------------------------- #
class ExplainResult(BaseModel):
    """Structured output of the terminal ``explain`` LLM call for ONE (role, scope) contradiction.

    ``kind`` is CLASSIFIED here (D11), not read from the auditor. ``granting_quotes`` /
    ``prohibiting_quotes`` are meant to be verbatim substrings of the candidate policy text; the
    engine validates each with :func:`_verify_quote` and, on any failure, keeps the conflict but
    sets ``quotes_verified=False`` and falls back to the auditor description for the explanation."""

    kind: ConflictKind
    granting_quotes: list[str] = []
    prohibiting_quotes: list[str] = []
    explanation: str = ""


# --------------------------------------------------------------------------- #
# Diagnostic state (extends the shared working state from graph.py).           #
# --------------------------------------------------------------------------- #
class DiagnosticState(_PRBWorking):
    """The live ``_PRBWorking`` fields (``policy_text``, ``selected_names``, ``denied_names``,
    ``conflict_names``, ``exclusive``, ``reasoning``, ``approved``, ``audit_feedback``,
    ``retry_count``, ``rules``) plus the diagnostic-only accumulators.

    ``focal_entities`` carries the resolved :class:`FocalEntitySet` for the run so the name->id
    join (D12) has the full typed context available; the join itself is performed against this
    run's own typed candidate lists (``scopes`` / ``roles``), which are the exact slice of that
    set that drove the run. ``recorded_contradictions`` is what the forked audit node writes
    instead of raising; ``conflicts`` / ``unevaluated`` are the report fragments this run emits."""

    focal_entities: FocalEntitySet | None
    recorded_contradictions: list[Any]  # list[Contradiction] recorded (not raised) by _audit_diagnostic
    conflicts: list[Conflict]
    unevaluated: list[Unevaluated]


class RoleDiagnosticState(DiagnosticState):
    role: Role
    scopes: list[Scope]


class ScopeDiagnosticState(DiagnosticState):
    roles: list[Role]
    scope: Scope


class DiagnosticResult(NamedTuple):
    """Per-entity output the survey (#158) collects and concatenates across all focal entities."""

    conflicts: list[Conflict]
    unevaluated: list[Unevaluated]


# --------------------------------------------------------------------------- #
# Pure quote validator (D5 + spec "Quote-validator rule").                     #
# --------------------------------------------------------------------------- #
def _normalize_ws(text: str) -> str:
    """Collapse every whitespace run (spaces, tabs, newlines) to a single space and trim the ends.
    ``str.split()`` with no separator splits on ANY run of whitespace, so ``" ".join(s.split())``
    performs exactly the specified normalization."""
    return " ".join(text.split())


def _verify_quote(quote: str, policy_text: str) -> bool:
    """True iff ``quote`` is a whitespace-normalized substring of ``policy_text``.

    Whitespace-normalize ONLY (both sides): collapse whitespace runs to a single space and trim.
    Deliberately NO case-folding and NO punctuation / smart-quote normalization -- the quote must
    be findable *as written* in the author's prose, so a near-quote (wrong case, curly vs straight
    quotes, altered punctuation) correctly FAILS and forces ``quotes_verified=False``."""
    return _normalize_ws(quote) in _normalize_ws(policy_text)


# --------------------------------------------------------------------------- #
# Forked nodes (record-not-raise audit; terminal explain; input-seeding START).#
# --------------------------------------------------------------------------- #
def _seed_policy_text(state: DiagnosticState) -> dict[str, Any]:
    """START node: seed ``policy_text`` from the INPUT state (candidate prose supplied by the
    caller). Deliberately does NOT call ``get_policy_source()`` -- this is the one place the live
    ``_fetch`` reads ``AIAC_POLICY_FILE`` and overwrites the input, which a pre-commit check on a
    *candidate* policy must not do (D14)."""
    return {"policy_text": state["policy_text"]}


def _audit_diagnostic(
    state: DiagnosticState,
    *,
    focal: str,
    candidates: str,
    direction: str,
    focal_ref: FocalRef,
) -> dict[str, Any]:
    """Forked audit node. Runs the SAME auditor call as the live ``_audit`` (same
    ``build_auditor_messages`` + ``AuditVerdict`` via ``_structured_call``) but ROUTES DIFFERENTLY:

      - genuine contradictions -> RECORD into state (never raise); route to ``explain``.
      - approved               -> no conflict for this entity; route to END.
      - ordinary rejection     -> feed the reason back and re-propose (<= MAX_AUDIT_RETRIES).
      - retry budget exhausted -> mark the entity ``unevaluated`` (nonconvergence); route to END.
    """
    verdict = _graph._structured_call(
        AuditVerdict,
        build_auditor_messages(
            state["policy_text"],
            focal,
            candidates,
            state["selected_names"],
            state["denied_names"],
            state["conflict_names"],
            direction=direction,
        ),
    )
    if verdict.contradictions:
        # RECORD, do not raise (the live path raises here). Routed to explain by _route_diagnostic.
        return {"recorded_contradictions": list(verdict.contradictions), "approved": False}
    if verdict.approved:
        return {"approved": True}
    if state["retry_count"] >= MAX_AUDIT_RETRIES:
        # Non-convergence: mark UNEVALUATED (do not raise) so a partial survey is never mistaken
        # for a clean one (its presence forces status away from no_conflict in #158).
        return {
            "approved": False,
            "unevaluated": [
                Unevaluated(
                    focal=focal_ref,
                    reason=UnevaluatedReason.NONCONVERGENCE,
                    detail=verdict.reason,
                )
            ],
        }
    # Ordinary rejection: thread the reason back and re-propose on the shared retry budget.
    return {"approved": False, "audit_feedback": verdict.reason, "retry_count": state["retry_count"] + 1}


def _explain(
    state: DiagnosticState,
    *,
    focal_type: FocalType,
    focal_obj: Role | Scope,
    candidate_by_name: dict[str, Role | Scope],
) -> dict[str, Any]:
    """Terminal node (D11). ONE ``_structured_call`` per recorded (role, scope) contradiction.

    Inputs to each call = candidate ``policy_text`` + the one (role, scope) pair + the auditor's
    ``description`` as a HINT ONLY (never the proposer's reasoning). Classifies ``kind``, extracts
    verbatim ``granting_quotes`` / ``prohibiting_quotes``, and validates each with
    :func:`_verify_quote`. On ANY quote failure (or no quotes at all) the conflict is KEPT with
    ``quotes_verified=False`` and the explanation falls back to the auditor ``description``.

    Name->id join + run-direction tagging (D12): the focal side is ``focal_obj`` (tagged with
    ``focal_type``); the candidate side is re-joined by name against this run's typed candidate
    set. A role-focal run => focal is the role; a scope-focal run => focal is the scope."""
    policy_text = state["policy_text"]
    focal_ref = FocalRef(name=focal_obj.name, id=focal_obj.id, type=focal_type)
    out: list[Conflict] = []
    for contradiction in state["recorded_contradictions"]:
        candidate = candidate_by_name.get(contradiction.candidate_name)
        if candidate is None:
            # Defensive: precheck already filtered names to the candidate set, so an unjoinable
            # name should not occur. Skip rather than emit a conflict with a fabricated id.
            continue
        if focal_type is FocalType.ROLE:
            role_obj, scope_obj = focal_obj, candidate
        else:
            role_obj, scope_obj = candidate, focal_obj

        result = _graph._structured_call(
            ExplainResult,
            build_explain_messages(
                policy_text,
                _role_focal(role_obj),
                _scope_focal(scope_obj),
                contradiction.description,
            ),
        )
        granting = list(result.granting_quotes)
        prohibiting = list(result.prohibiting_quotes)
        # Verified only when there is at least one quote AND every quote is a verbatim substring.
        verified = bool(granting or prohibiting) and all(
            _verify_quote(q, policy_text) for q in granting + prohibiting
        )
        explanation = result.explanation if verified else contradiction.description
        out.append(
            Conflict(
                focal=focal_ref,
                role=EntityRef(name=role_obj.name, id=role_obj.id),
                scope=EntityRef(name=scope_obj.name, id=scope_obj.id),
                kind=result.kind,
                granting_quotes=granting,
                prohibiting_quotes=prohibiting,
                explanation=explanation,
                quotes_verified=verified,
            )
        )
    return {"conflicts": out}


def _route_diagnostic(state: DiagnosticState) -> str:
    """Route out of the audit node. Recorded contradictions -> explain; a clean approval or a
    non-convergence mark -> END; anything else (ordinary rejection with budget left) -> retry."""
    if state.get("recorded_contradictions"):
        return "explain"
    if state.get("approved"):
        return "end"
    if state.get("unevaluated"):
        return "end"
    return "retry"


# --------------------------------------------------------------------------- #
# Assembly (parallel to graph._assemble; no build node; record-not-raise).     #
# --------------------------------------------------------------------------- #
def _assemble_diagnostic(state_type: type, seed, propose, precheck, audit_diagnostic, explain):
    """Wire START->seed->propose->precheck->audit_diagnostic->{explain | retry->propose | END}.
    Mirrors ``graph._assemble`` but swaps ``_fetch`` for the input-seeding START node, drops the
    ``build`` node, and replaces the two-way approved/rejected route with the three-way
    explain/retry/end route of the record-not-raise audit node."""
    g = StateGraph(state_type)
    g.add_node("seed", seed)
    g.add_node("propose", propose)
    g.add_node("precheck", precheck)
    g.add_node("audit", audit_diagnostic)
    g.add_node("explain", explain)
    g.add_edge(START, "seed")
    g.add_edge("seed", "propose")
    g.add_edge("propose", "precheck")
    g.add_edge("precheck", "audit")
    g.add_conditional_edges("audit", _route_diagnostic, {"explain": "explain", "retry": "propose", "end": END})
    g.add_edge("explain", END)
    return g.compile()


def build_role_diagnostic_graph():
    """Role-focal diagnostic run: the focal entity is a ROLE; the candidates are SCOPES."""

    def seed(s: RoleDiagnosticState) -> dict[str, Any]:
        return _seed_policy_text(s)

    def propose(s: RoleDiagnosticState) -> dict[str, Any]:
        return _propose(
            s,
            focal=_role_focal(s["role"]),
            candidates=_scope_cands(s["scopes"]),
            contract=_ROLE_CONTRACT,
            direction=_ROLE_DIRECTION,
            schema=RoleSelection,
            names_field="granted_scope_names",
            denied_names_field="denied_scope_names",
            exclusive_field="grant_is_exclusive",
        )

    def precheck(s: RoleDiagnosticState) -> dict[str, Any]:
        return _precheck(s, candidate_names={sc.name for sc in s["scopes"]})

    def audit(s: RoleDiagnosticState) -> dict[str, Any]:
        return _audit_diagnostic(
            s,
            focal=_role_focal(s["role"]),
            candidates=_scope_cands(s["scopes"]),
            direction=_ROLE_DIRECTION,
            focal_ref=FocalRef(name=s["role"].name, id=s["role"].id, type=FocalType.ROLE),
        )

    def explain(s: RoleDiagnosticState) -> dict[str, Any]:
        return _explain(
            s,
            focal_type=FocalType.ROLE,
            focal_obj=s["role"],
            candidate_by_name={sc.name: sc for sc in s["scopes"]},
        )

    return _assemble_diagnostic(RoleDiagnosticState, seed, propose, precheck, audit, explain)


def build_scope_diagnostic_graph():
    """Scope-focal diagnostic run: the focal entity is a SCOPE; the candidates are ROLES."""

    def seed(s: ScopeDiagnosticState) -> dict[str, Any]:
        return _seed_policy_text(s)

    def propose(s: ScopeDiagnosticState) -> dict[str, Any]:
        return _propose(
            s,
            focal=_scope_focal(s["scope"]),
            candidates=_role_cands(s["roles"]),
            contract=_SCOPE_CONTRACT,
            direction=_SCOPE_DIRECTION,
            schema=ScopeSelection,
            names_field="roles_with_access_names",
            denied_names_field="roles_denied_access_names",
            exclusive_field="access_is_exclusive",
        )

    def precheck(s: ScopeDiagnosticState) -> dict[str, Any]:
        return _precheck(s, candidate_names={r.name for r in s["roles"]})

    def audit(s: ScopeDiagnosticState) -> dict[str, Any]:
        return _audit_diagnostic(
            s,
            focal=_scope_focal(s["scope"]),
            candidates=_role_cands(s["roles"]),
            direction=_SCOPE_DIRECTION,
            focal_ref=FocalRef(name=s["scope"].name, id=s["scope"].id, type=FocalType.SCOPE),
        )

    def explain(s: ScopeDiagnosticState) -> dict[str, Any]:
        return _explain(
            s,
            focal_type=FocalType.SCOPE,
            focal_obj=s["scope"],
            candidate_by_name={r.name: r for r in s["roles"]},
        )

    return _assemble_diagnostic(ScopeDiagnosticState, seed, propose, precheck, audit, explain)


# Module-level compile is safe (never builds the LLM), mirroring ROLE_GRAPH / SCOPE_GRAPH.
ROLE_DIAGNOSTIC_GRAPH = build_role_diagnostic_graph()
SCOPE_DIAGNOSTIC_GRAPH = build_scope_diagnostic_graph()


def _base_state(policy_text: str, focal_entities: FocalEntitySet | None) -> dict[str, Any]:
    return {
        "policy_text": policy_text,
        "selected_names": [],
        "denied_names": [],
        "conflict_names": [],
        "exclusive": False,
        "reasoning": "",
        "approved": False,
        "audit_feedback": None,
        "retry_count": 0,
        "rules": [],
        "focal_entities": focal_entities,
        "recorded_contradictions": [],
        "conflicts": [],
        "unevaluated": [],
    }


def run_role_diagnostic(
    policy_text: str,
    role: Role,
    scopes: list[Scope],
    *,
    focal_entities: FocalEntitySet | None = None,
) -> DiagnosticResult:
    """Run ONE role-focal diagnostic entity end-to-end and return its conflicts + unevaluated.

    ``role`` is the focal entity (one ``own_role``); ``scopes`` are the candidate scopes it is
    checked against (``other_scopes`` in the resolver fan-out). ``policy_text`` is the candidate
    prose (seeded directly -- no file read). Never raises on a genuine conflict or non-convergence;
    both are returned in the :class:`DiagnosticResult` for #158 to accumulate."""
    state: RoleDiagnosticState = {**_base_state(policy_text, focal_entities), "role": role, "scopes": scopes}  # type: ignore[assignment]
    out = ROLE_DIAGNOSTIC_GRAPH.invoke(state)
    return DiagnosticResult(conflicts=out["conflicts"], unevaluated=out["unevaluated"])


def run_scope_diagnostic(
    policy_text: str,
    roles: list[Role],
    scope: Scope,
    *,
    focal_entities: FocalEntitySet | None = None,
) -> DiagnosticResult:
    """Run ONE scope-focal diagnostic entity end-to-end and return its conflicts + unevaluated.

    ``scope`` is the focal entity (one ``own_scope``); ``roles`` are the candidate roles it is
    checked against (``candidate_roles`` in the resolver fan-out). ``policy_text`` is the candidate
    prose (seeded directly -- no file read). Never raises on a genuine conflict or non-convergence;
    both are returned in the :class:`DiagnosticResult` for #158 to accumulate."""
    state: ScopeDiagnosticState = {**_base_state(policy_text, focal_entities), "roles": roles, "scope": scope}  # type: ignore[assignment]
    out = SCOPE_DIAGNOSTIC_GRAPH.invoke(state)
    return DiagnosticResult(conflicts=out["conflicts"], unevaluated=out["unevaluated"])
