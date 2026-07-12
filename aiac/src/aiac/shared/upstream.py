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

from tenacity import Retrying, stop_after_attempt, wait_exponential

T = TypeVar("T")


def run_upstream(fn: Callable[[], T]) -> T:
    """Run ``fn`` with bounded retries (``UPSTREAM_MAX_RETRIES``, default 3) and exponential
    backoff, reraising the last error so the caller can convert it to a 502. Returns whatever
    ``fn`` returns (the return type is preserved for callers)."""
    retryer = Retrying(
        stop=stop_after_attempt(int(os.getenv("UPSTREAM_MAX_RETRIES", "3"))),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        reraise=True,
    )
    return retryer(fn)
