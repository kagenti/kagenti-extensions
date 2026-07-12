"""Shared upstream-call helper for the agent layer.

`run_upstream` wraps a zero-arg callable in a bounded retry loop for transient upstream
failures (IdP Configuration Service, Kubernetes API, MCP endpoints). It is deliberately
transport-agnostic: it retries then **re-raises the original exception**, leaving each
caller to map the failure to the right HTTP status at the service boundary (e.g.
``HTTPException(502)`` for IdP/Kubernetes — see the Error Handling table in
``aiac-agent.md``). The retry budget is read from ``UPSTREAM_MAX_RETRIES`` (default 3) at
call time, so tests can tune it via the environment.

Consolidates the retry helper that was previously inlined in each onboarding sub-agent;
callers today are the UC1 Provision (``provision/nodes.py``) and Service Policy
(``service_policy/runner.py``) sub-agents.
"""

import os

from tenacity import Retrying, stop_after_attempt, wait_exponential


def run_upstream(fn):
    """Run ``fn`` with bounded retries (``UPSTREAM_MAX_RETRIES``, default 3) and exponential
    backoff, reraising the last error so the caller can convert it to a 502."""
    retryer = Retrying(
        stop=stop_after_attempt(int(os.getenv("UPSTREAM_MAX_RETRIES", "3"))),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        reraise=True,
    )
    return retryer(fn)
