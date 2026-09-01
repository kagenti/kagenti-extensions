"""Deterministic unit tests for the read-only Conflict-Check diagnostic engine (#157).

Every LLM turn -- proposer, auditor, and the terminal explain call -- flows through the SAME
``graph._structured_call`` seam the live path uses, so ONE ``side_effect`` list drives all three
in order (mirrors ``test_graph.py``). No live endpoint is touched and, unlike the live graph, the
diagnostic seeds ``policy_text`` from input, so ``get_policy_source`` is never patched here.

Coverage: single-entity end-to-end RECORDING (not raising) a genuine contradiction -> classified
Conflict with verbatim quotes (role + scope directions); retry-budget exhaustion -> unevaluated
(no raise); explain kind classification; substring-validation failure -> quotes_verified=False +
description fallback; and ``_verify_quote`` as a pure function.
"""

from contextlib import ExitStack
from unittest.mock import patch

from aiac.agent.policy_rules_builder.diagnostic import (
    ExplainResult,
    _verify_quote,
    run_role_diagnostic,
    run_scope_diagnostic,
)
from aiac.agent.policy_rules_builder.diagnostic_models import ConflictKind, FocalType
from aiac.agent.policy_rules_builder.graph import AuditVerdict, Contradiction, RoleSelection, ScopeSelection
from aiac.idp.configuration.models import Role, Scope

_SEAM = "aiac.agent.policy_rules_builder.graph._structured_call"


def _role(id="r-dev", name="developer") -> Role:
    return Role(id=id, name=name, composite=False, childRoles=[])


def _scope(id="s-iss", name="issues") -> Scope:
    return Scope(id=id, name=name)


def _patch_calls(stack: ExitStack, side_effect):
    return stack.enter_context(patch(_SEAM, side_effect=side_effect))


# --------------------------------------------------------------------------- #
# 1 — role-focal run: a genuine contradiction is RECORDED (not raised) and     #
#     turned into a classified Conflict with verbatim, validated quotes.       #
# --------------------------------------------------------------------------- #
def test_role_run_records_genuine_conflict_with_verbatim_quotes():
    role = _role("r-dev", "developer")
    issues = _scope("s-iss", "issues")
    policy = "Developers may read issues.\nDevelopers must not modify issues."

    with ExitStack() as stack:
        _patch_calls(
            stack,
            [
                RoleSelection(
                    granted_scope_names=["issues"],
                    denied_scope_names=["issues"],
                    reasoning="may read but must not modify",
                ),
                AuditVerdict(
                    approved=False,
                    contradictions=[
                        Contradiction(candidate_name="issues", description="coarse-scope granularity mismatch")
                    ],
                ),
                ExplainResult(
                    kind=ConflictKind.COARSE_SCOPE,
                    granting_quotes=["Developers may read issues."],
                    prohibiting_quotes=["Developers must not modify issues."],
                    explanation="issues covers both read and write; reading is granted but writing is forbidden",
                ),
            ],
        )
        result = run_role_diagnostic(policy, role, [issues])

    assert result.unevaluated == []
    assert len(result.conflicts) == 1
    c = result.conflicts[0]
    assert c.kind is ConflictKind.COARSE_SCOPE
    assert c.quotes_verified is True
    assert c.focal.type is FocalType.ROLE
    assert (c.focal.name, c.focal.id) == ("developer", "r-dev")
    assert (c.role.name, c.role.id) == ("developer", "r-dev")
    assert (c.scope.name, c.scope.id) == ("issues", "s-iss")
    assert c.granting_quotes == ["Developers may read issues."]
    assert c.prohibiting_quotes == ["Developers must not modify issues."]
    # Every reported quote is a verbatim substring of the candidate policy text.
    for q in c.granting_quotes + c.prohibiting_quotes:
        assert _verify_quote(q, policy)


# --------------------------------------------------------------------------- #
# 2 — scope-focal run: the direction flips (focal is the SCOPE, candidate is    #
#     the ROLE), and a DIRECT conflict is classified as such.                  #
# --------------------------------------------------------------------------- #
def test_scope_run_records_conflict_direction_flipped_and_direct_kind():
    scope = _scope("s-audit", "audit-log")
    intern = _role("r-int", "intern")
    policy = "Interns may access the audit-log. Interns may not access the audit-log."

    with ExitStack() as stack:
        _patch_calls(
            stack,
            [
                ScopeSelection(
                    roles_with_access_names=["intern"],
                    roles_denied_access_names=["intern"],
                    reasoning="granted and forbidden for interns",
                ),
                AuditVerdict(
                    approved=False,
                    contradictions=[Contradiction(candidate_name="intern", description="direct conflict")],
                ),
                ExplainResult(
                    kind=ConflictKind.DIRECT,
                    granting_quotes=["Interns may access the audit-log."],
                    prohibiting_quotes=["Interns may not access the audit-log."],
                    explanation="the same access is both granted and prohibited",
                ),
            ],
        )
        result = run_scope_diagnostic(policy, [intern], scope)

    assert result.unevaluated == []
    assert len(result.conflicts) == 1
    c = result.conflicts[0]
    assert c.kind is ConflictKind.DIRECT
    assert c.quotes_verified is True
    assert c.focal.type is FocalType.SCOPE
    assert (c.focal.name, c.focal.id) == ("audit-log", "s-audit")
    assert (c.scope.name, c.scope.id) == ("audit-log", "s-audit")
    assert (c.role.name, c.role.id) == ("intern", "r-int")


# --------------------------------------------------------------------------- #
# 3 — a clean approval records NO conflict and NO unevaluated (routes to END    #
#     without visiting explain).                                               #
# --------------------------------------------------------------------------- #
def test_clean_approval_records_nothing():
    role = _role("r-dev", "developer")
    issues = _scope("s-iss", "issues")

    with ExitStack() as stack:
        _patch_calls(
            stack,
            [
                RoleSelection(granted_scope_names=["issues"], reasoning="granted"),
                AuditVerdict(approved=True),
            ],
        )
        result = run_role_diagnostic("Developers may use issues.", role, [issues])

    assert result.conflicts == []
    assert result.unevaluated == []


# --------------------------------------------------------------------------- #
# 4 — retry-budget exhaustion marks the entity UNEVALUATED (nonconvergence)     #
#     and does NOT raise; no conflicts are produced.                           #
# --------------------------------------------------------------------------- #
def test_retry_budget_exhaustion_marks_unevaluated_no_raise():
    role = _role("r-dev", "developer")
    issues = _scope("s-iss", "issues")

    def se(schema, messages):
        if schema is AuditVerdict:
            return AuditVerdict(approved=False, reason="still not right")
        return RoleSelection(granted_scope_names=["issues"], reasoning="r")

    with ExitStack() as stack:
        _patch_calls(stack, se)
        result = run_role_diagnostic("Some policy.", role, [issues])

    assert result.conflicts == []
    assert len(result.unevaluated) == 1
    u = result.unevaluated[0]
    assert u.reason.value == "nonconvergence"
    assert u.focal.type is FocalType.ROLE
    assert (u.focal.name, u.focal.id) == ("developer", "r-dev")
    assert u.detail == "still not right"


# --------------------------------------------------------------------------- #
# 5 — substring-validation FAILURE: the explain call returns a non-substring    #
#     granting quote, so the conflict is KEPT with quotes_verified=False and    #
#     the explanation falls back to the auditor description.                    #
# --------------------------------------------------------------------------- #
def test_quote_validation_failure_sets_unverified_and_falls_back_to_description():
    role = _role("r-dev", "developer")
    issues = _scope("s-iss", "issues")
    policy = "Developers may read issues.\nDevelopers must not modify issues."
    audit_desc = "coarse-scope granularity mismatch on issues"

    with ExitStack() as stack:
        _patch_calls(
            stack,
            [
                RoleSelection(
                    granted_scope_names=["issues"],
                    denied_scope_names=["issues"],
                    reasoning="r",
                ),
                AuditVerdict(
                    approved=False,
                    contradictions=[Contradiction(candidate_name="issues", description=audit_desc)],
                ),
                ExplainResult(
                    kind=ConflictKind.COARSE_SCOPE,
                    # Not a verbatim substring of the policy (paraphrased) -> validation fails.
                    granting_quotes=["Developers are allowed to read every issue"],
                    prohibiting_quotes=["Developers must not modify issues."],
                    explanation="a model paraphrase that must NOT survive fallback",
                ),
            ],
        )
        result = run_role_diagnostic(policy, role, [issues])

    assert len(result.conflicts) == 1
    c = result.conflicts[0]
    assert c.quotes_verified is False
    assert c.explanation == audit_desc  # fell back to the auditor description, not the model prose
    assert c.kind is ConflictKind.COARSE_SCOPE  # classification is still kept


# --------------------------------------------------------------------------- #
# 6 — empty quotes are treated as an unverified citation (kept, fallback).      #
# --------------------------------------------------------------------------- #
def test_empty_quotes_are_unverified_and_fall_back():
    role = _role("r-dev", "developer")
    issues = _scope("s-iss", "issues")
    audit_desc = "direct conflict on issues"

    with ExitStack() as stack:
        _patch_calls(
            stack,
            [
                RoleSelection(granted_scope_names=["issues"], denied_scope_names=["issues"], reasoning="r"),
                AuditVerdict(
                    approved=False,
                    contradictions=[Contradiction(candidate_name="issues", description=audit_desc)],
                ),
                ExplainResult(kind=ConflictKind.DIRECT, granting_quotes=[], prohibiting_quotes=[], explanation="x"),
            ],
        )
        result = run_role_diagnostic("Developers policy about issues.", role, [issues])

    c = result.conflicts[0]
    assert c.quotes_verified is False
    assert c.explanation == audit_desc


# --------------------------------------------------------------------------- #
# 7 — _verify_quote as a pure function: substring match, whitespace            #
#     normalization (incl. newlines), non-match, and NO case-folding.          #
# --------------------------------------------------------------------------- #
def test_verify_quote_exact_substring():
    assert _verify_quote("must not modify", "Developers must not modify issues.")


def test_verify_quote_normalizes_whitespace_runs_and_newlines():
    policy = "Developers   may\tread\nissues."
    # Multiple spaces, a tab, and a newline in the source all collapse to single spaces.
    assert _verify_quote("Developers may read issues.", policy)
    # A multiline quote also normalizes to match single-spaced source prose.
    assert _verify_quote("read\n  issues.", "Developers may read issues.")


def test_verify_quote_rejects_non_substring():
    assert not _verify_quote("Developers may deploy to prod", "Developers may read issues.")


def test_verify_quote_is_case_sensitive():
    # No case-folding: a wrong-case near-quote must FAIL (it is not findable as written).
    assert not _verify_quote("developers may read issues.", "Developers may read issues.")
