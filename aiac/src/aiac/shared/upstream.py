"""Shared upstream-call helper (project-level).

`run_upstream` wraps a zero-arg callable in a bounded retry loop for transient upstream
failures (IdP Configuration Service, Kubernetes API, MCP endpoints). It is deliberately
transport-agnostic: it retries then **re-raises the original exception**, leaving each
caller to map the failure to the right HTTP status at the service boundary (e.g.
``HTTPException(502)`` for IdP/Kubernetes — see the Error Handling table in
``aiac-agent.md``). The retry budget is read from ``UPSTREAM_MAX_RETRIES`` (default 3) at
call time, so tests can tune it via the environment.

Lives at the project root (``aiac.shared``) so any layer can reuse it without importing from
the ``agent`` package. Retry now lives at the transport boundary: the idp-library
``Configuration`` methods, the ``_mcp_tools_list`` helper, and the provision k8s seam
(``provision/kube.py``) each call it internally so the agent nodes no longer orchestrate
retries.
"""

import os
from typing import Callable, TypeVar

from tenacity import Retrying, retry_if_exception, stop_after_attempt, wait_exponential

T = TypeVar("T")

_DEFAULT_MAX_RETRIES = 3


def max_retries() -> int:
    """Retry budget from ``UPSTREAM_MAX_RETRIES`` (default 3), tolerant of an unset or
    non-numeric value — a bad value must not crash the request, it falls back to the default."""
    try:
        value = int(os.getenv("UPSTREAM_MAX_RETRIES", str(_DEFAULT_MAX_RETRIES)))
    except (TypeError, ValueError):
        return _DEFAULT_MAX_RETRIES
    return value if value > 0 else _DEFAULT_MAX_RETRIES


def _status_code(exc: BaseException) -> int | None:
    """Best-effort HTTP status of an exception: ``requests`` HTTPError carries it on
    ``.response.status_code``; a Kubernetes ``ApiException`` carries it on ``.status``."""
    resp = getattr(exc, "response", None)
    for candidate in (getattr(resp, "status_code", None), getattr(exc, "status", None)):
        try:
            return int(candidate)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
    return None


def is_transient(exc: BaseException) -> bool:
    """Only transient upstream failures are worth retrying: connection resets, timeouts, or a
    5xx server response. Permanent failures (4xx, value/validation errors) fail identically on
    every attempt, so they are surfaced immediately rather than retried. Duck-typed so it
    covers ``requests`` and Kubernetes client errors without importing either here."""
    if isinstance(exc, (ConnectionError, TimeoutError)):  # builtins (also OSError subclasses)
        return True
    # requests.exceptions.ConnectionError / Timeout are NOT subclasses of the builtins above;
    # match them structurally by class name so this module stays transport-agnostic.
    name = type(exc).__name__
    if name in (
        "ConnectionError",
        "Timeout",
        "ConnectTimeout",
        "ReadTimeout",
        "ConnectionResetError",
        # openai / langchain_openai raise these on request timeout / dropped connection;
        # matched by name so this module keeps no openai import (transport-agnostic).
        "APITimeoutError",
        "APIConnectionError",
    ):
        return True
    status = _status_code(exc)
    return status is not None and status >= 500


def run_upstream(fn: Callable[[], T]) -> T:
    """Run ``fn`` with bounded retries (``UPSTREAM_MAX_RETRIES``, default 3) and exponential
    backoff, retrying **only transient failures** (``is_transient``) and reraising the last
    error so the caller can convert it to a 502. Returns whatever ``fn`` returns (the return
    type is preserved for callers)."""
    retryer = Retrying(
        retry=retry_if_exception(is_transient),
        stop=stop_after_attempt(max_retries()),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        reraise=True,
    )
    return retryer(fn)
