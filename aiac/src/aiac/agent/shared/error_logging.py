"""Per-persona named-logger router for PRB exceptions (#169).

``log_by_type(exc)`` is the single place both the Controller exception handlers (#172)
and the NATS consumer (#173) log a Policy Rules Builder failure. It routes each exception
**type** to its own named logger so downstream aggregation can split by logger name, and
logs the FULL context — the exception's (already-sanitized) message plus its traceback and
root cause (via ``exc_info``) — to **stdout** through the stdlib ``logging`` module. There
are no per-exception files: the container rootfs is read-only and ``/tmp`` is ephemeral, so
persistence is the log pipeline's job, not this router's.

The four PRB errors share a base but are siblings (none inherits from another); the
report-carrying ``PolicyConflictError`` is a separate class. Both grant/deny findings
(``PolicyContradictionError`` intra-pass, ``PolicyConflictError`` cross-pass) aggregate on
the same ``aiac.onboarding.contradiction`` logger. The type→logger mapping is the authority
here — it does not recompute anything the callers do.
"""

import logging

from aiac.agent.policy_rules_builder.conflict_detection import PolicyConflictError
from aiac.agent.policy_rules_builder.graph import (
    LLMAccessError,
    PolicyContradictionError,
    PolicyRulesBuilderError,
    UnparseableLLMResponseError,
)

# Per-persona named loggers. Each exception TYPE maps to exactly one logger name so a
# downstream collector can split the stream by ``record.name``. Ordered most-specific
# first; ``log_by_type`` walks the exception's MRO against this table, so a subclass of a
# mapped type still lands on its ancestor's logger.
_LOGGER_BY_TYPE: dict[type[BaseException], str] = {
    LLMAccessError: "aiac.onboarding.llm_access",
    UnparseableLLMResponseError: "aiac.onboarding.llm_response",
    PolicyContradictionError: "aiac.onboarding.contradiction",
    PolicyConflictError: "aiac.onboarding.contradiction",
    PolicyRulesBuilderError: "aiac.onboarding.builder",
}

# Anything not in the table (e.g. a bare ``PolicyRulesBuilderBaseError`` safety-net or a
# genuinely unknown/transient error the consumer still routes through here) lands on the
# parent namespace so it is never dropped silently.
_FALLBACK_LOGGER = "aiac.onboarding"


def _logger_name_for(exc: BaseException) -> str:
    for klass in type(exc).__mro__:
        name = _LOGGER_BY_TYPE.get(klass)
        if name is not None:
            return name
    return _FALLBACK_LOGGER


def log_by_type(exc: BaseException) -> None:
    """Log ``exc`` on its per-persona named logger with full context.

    Emits exactly ONE ``ERROR`` record — the handler and the consumer are mutually
    exclusive per request, so the router itself never double-logs. The message carries the
    exception class name and its sanitized summary; the traceback and chained root cause
    ride along on ``exc_info`` for the operator, never in the caller's HTTP response body.
    """
    logging.getLogger(_logger_name_for(exc)).error(
        "%s: %s", type(exc).__name__, exc, exc_info=exc
    )
