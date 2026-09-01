"""Unit tests for the policy conflict-check diagnostic models (#156)."""

import pytest
from pydantic import ValidationError

from aiac.agent.policy_rules_builder.diagnostic_models import (
    Conflict,
    ConflictKind,
    ConflictReport,
    ConflictStatus,
    EntityRef,
    FocalRef,
    FocalType,
    Unevaluated,
    UnevaluatedReason,
)


# --- enum values (pinned exactly) -------------------------------------------------------------


def test_focal_type_values():
    assert [m.value for m in FocalType] == ["role", "scope"]
    assert FocalType.ROLE == "role"  # str-enum identity


def test_conflict_kind_values():
    assert [m.value for m in ConflictKind] == ["direct", "coarse_scope"]
    assert ConflictKind.COARSE_SCOPE == "coarse_scope"


def test_status_values_exactly():
    assert {m.value for m in ConflictStatus} == {"no_conflict", "conflicts_found", "incomplete"}


def test_unevaluated_reason_values():
    assert [m.value for m in UnevaluatedReason] == ["nonconvergence"]


# --- ref models -------------------------------------------------------------------------------


def test_entity_ref_shape():
    ref = EntityRef(name="reader", id="r-1")
    assert ref.model_dump() == {"name": "reader", "id": "r-1"}


def test_focal_ref_is_entity_ref_plus_type():
    focal = FocalRef(name="reader", id="r-1", type=FocalType.ROLE)
    assert isinstance(focal, EntityRef)
    assert focal.model_dump() == {"name": "reader", "id": "r-1", "type": "role"}


def test_focal_ref_type_is_constrained():
    with pytest.raises(ValidationError):
        FocalRef(name="x", id="y", type="subject")


# --- Conflict ---------------------------------------------------------------------------------


def _conflict(**over):
    base = dict(
        focal=FocalRef(name="reader", id="r-1", type=FocalType.ROLE),
        role=EntityRef(name="reader", id="r-1"),
        scope=EntityRef(name="repo:write", id="s-1"),
        kind=ConflictKind.DIRECT,
        granting_quotes=["reader may write"],
        prohibiting_quotes=["reader must not write"],
        explanation="granted and prohibited the same pair",
        quotes_verified=True,
    )
    base.update(over)
    return Conflict(**base)


def test_conflict_full_shape_serializes():
    dumped = _conflict().model_dump()
    assert dumped == {
        "focal": {"name": "reader", "id": "r-1", "type": "role"},
        "role": {"name": "reader", "id": "r-1"},
        "scope": {"name": "repo:write", "id": "s-1"},
        "kind": "direct",
        "granting_quotes": ["reader may write"],
        "prohibiting_quotes": ["reader must not write"],
        "explanation": "granted and prohibited the same pair",
        "quotes_verified": True,
    }


def test_conflict_quotes_default_empty():
    c = Conflict(
        focal=FocalRef(name="s", id="s-1", type=FocalType.SCOPE),
        role=EntityRef(name="r", id="r-1"),
        scope=EntityRef(name="s", id="s-1"),
        kind=ConflictKind.COARSE_SCOPE,
        explanation="e",
        quotes_verified=False,
    )
    assert c.granting_quotes == []
    assert c.prohibiting_quotes == []


def test_quotes_are_list_of_str():
    assert _conflict().granting_quotes == ["reader may write"]
    assert isinstance(_conflict().prohibiting_quotes, list)


# --- Unevaluated ------------------------------------------------------------------------------


def test_unevaluated_defaults_reason_and_optional_detail():
    u = Unevaluated(focal=FocalRef(name="r", id="r-1", type=FocalType.ROLE))
    assert u.reason == UnevaluatedReason.NONCONVERGENCE
    assert u.detail is None
    assert u.model_dump() == {
        "focal": {"name": "r", "id": "r-1", "type": "role"},
        "reason": "nonconvergence",
        "detail": None,
    }


def test_unevaluated_with_detail():
    u = Unevaluated(
        focal=FocalRef(name="r", id="r-1", type=FocalType.ROLE),
        detail="exhausted 3 retries",
    )
    assert u.detail == "exhausted 3 retries"


# --- ConflictReport ---------------------------------------------------------------------------


def test_report_empty_defaults():
    report = ConflictReport(status=ConflictStatus.NO_CONFLICT)
    assert report.conflicts == []
    assert report.unevaluated == []
    dumped = report.model_dump()
    assert dumped["status"] == "no_conflict"
    assert dumped["conflicts"] == []
    assert dumped["unevaluated"] == []


def test_report_round_trips_via_json():
    report = ConflictReport.from_survey([_conflict()], [], evaluated_count=1)
    reloaded = ConflictReport.model_validate_json(report.model_dump_json())
    assert reloaded == report


# --- derive_status precedence -----------------------------------------------------------------


def test_derive_status_no_conflict():
    assert (
        ConflictReport.derive_status([], [], evaluated_count=3) == ConflictStatus.NO_CONFLICT
    )


def test_derive_status_conflicts_found_takes_precedence_over_unevaluated():
    u = Unevaluated(focal=FocalRef(name="r", id="r-1", type=FocalType.ROLE))
    # conflicts win even when there are also unevaluated entities
    assert (
        ConflictReport.derive_status([_conflict()], [u], evaluated_count=1)
        == ConflictStatus.CONFLICTS_FOUND
    )


def test_derive_status_incomplete_when_zero_evaluated():
    assert ConflictReport.derive_status([], [], evaluated_count=0) == ConflictStatus.INCOMPLETE


def test_derive_status_incomplete_when_unevaluated_present():
    u = Unevaluated(focal=FocalRef(name="r", id="r-1", type=FocalType.ROLE))
    assert (
        ConflictReport.derive_status([], [u], evaluated_count=5) == ConflictStatus.INCOMPLETE
    )


def test_from_survey_encodes_precedence():
    assert ConflictReport.from_survey([], [], 0).status == ConflictStatus.INCOMPLETE
    assert ConflictReport.from_survey([], [], 2).status == ConflictStatus.NO_CONFLICT
    assert ConflictReport.from_survey([_conflict()], [], 1).status == ConflictStatus.CONFLICTS_FOUND
