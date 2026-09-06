"""Tests for the per-persona named-logger router ``log_by_type`` (#169).

Seam under test: given a PRB exception instance, ``log_by_type(exc)`` emits exactly one
log record on the EXPECTED named logger at the expected level, with the traceback
attached (``exc_info``) and the exception's context in the message. Assertions are on the
public ``logging`` surface only (``caplog`` records) — no private-attribute checks.
"""

import logging

from aiac.agent.policy_rules_builder.conflict_detection import PolicyConflictError
from aiac.agent.policy_rules_builder.diagnostic_models import ConflictReport
from aiac.agent.policy_rules_builder.graph import (
    Contradiction,
    LLMAccessError,
    PolicyContradictionError,
    PolicyRulesBuilderError,
    UnparseableLLMResponseError,
)
from aiac.agent.shared.error_logging import log_by_type


def _onboarding_records(caplog):
    return [r for r in caplog.records if r.name.startswith("aiac.onboarding")]


def test_builder_error_routes_to_builder_logger(caplog):
    with caplog.at_level(logging.DEBUG):
        try:
            raise PolicyRulesBuilderError("auditor rejected after retries")
        except PolicyRulesBuilderError as exc:
            log_by_type(exc)

    records = _onboarding_records(caplog)
    assert len(records) == 1
    assert records[0].name == "aiac.onboarding.builder"
    assert records[0].levelno == logging.ERROR


def test_llm_access_error_routes_to_llm_access_logger(caplog):
    with caplog.at_level(logging.DEBUG):
        try:
            raise LLMAccessError("LLM endpoint unreachable")
        except LLMAccessError as exc:
            log_by_type(exc)

    records = _onboarding_records(caplog)
    assert len(records) == 1
    assert records[0].name == "aiac.onboarding.llm_access"
    assert records[0].levelno == logging.ERROR


def test_unparseable_llm_response_error_routes_to_llm_response_logger(caplog):
    with caplog.at_level(logging.DEBUG):
        try:
            raise UnparseableLLMResponseError("schema validation failed")
        except UnparseableLLMResponseError as exc:
            log_by_type(exc)

    records = _onboarding_records(caplog)
    assert len(records) == 1
    assert records[0].name == "aiac.onboarding.llm_response"
    assert records[0].levelno == logging.ERROR


def test_policy_contradiction_error_routes_to_contradiction_logger(caplog):
    contradictions = [Contradiction(candidate_name="reader", description="grant vs deny")]
    with caplog.at_level(logging.DEBUG):
        try:
            raise PolicyContradictionError("github-agent", contradictions)
        except PolicyContradictionError as exc:
            log_by_type(exc)

    records = _onboarding_records(caplog)
    assert len(records) == 1
    assert records[0].name == "aiac.onboarding.contradiction"
    assert records[0].levelno == logging.ERROR


def test_policy_conflict_error_routes_to_contradiction_logger(caplog):
    # The report-carrying cross-pass conflict aggregates on the SAME contradiction logger.
    report = ConflictReport.from_survey([], [], evaluated_count=0)
    with caplog.at_level(logging.DEBUG):
        try:
            raise PolicyConflictError(report)
        except PolicyConflictError as exc:
            log_by_type(exc)

    records = _onboarding_records(caplog)
    assert len(records) == 1
    assert records[0].name == "aiac.onboarding.contradiction"
    assert records[0].levelno == logging.ERROR


def test_full_context_is_logged_with_traceback_and_message(caplog):
    with caplog.at_level(logging.DEBUG):
        try:
            raise LLMAccessError("LLM endpoint unreachable")
        except LLMAccessError as exc:
            log_by_type(exc)

    record = _onboarding_records(caplog)[0]
    # Traceback / root cause is attached via exc_info (type, value, traceback).
    assert record.exc_info is not None
    assert record.exc_info[0] is LLMAccessError
    assert record.exc_info[2] is not None  # a real traceback object, not None
    # The exception's own context (its sanitized summary) rides in the message.
    assert "LLM endpoint unreachable" in record.getMessage()


def test_no_double_logging_single_record_per_call(caplog):
    with caplog.at_level(logging.DEBUG):
        try:
            raise PolicyRulesBuilderError("auditor rejected after retries")
        except PolicyRulesBuilderError as exc:
            log_by_type(exc)

    # Exactly one record — the handler and the consumer are mutually exclusive per request,
    # so the router itself must never emit duplicates.
    assert len(_onboarding_records(caplog)) == 1
