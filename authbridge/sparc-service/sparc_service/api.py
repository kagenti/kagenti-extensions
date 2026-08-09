"""FastAPI application for the SPARC reflection service.

Endpoints:
  POST /reflect  — run SPARC on a proposed tool call, return the verdict.
  GET  /healthz  — liveness (always ok if the process is up).
  GET  /readyz   — readiness (config valid and component buildable).
"""

from __future__ import annotations

import json
import logging

from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool

from .engine import ReflectionEngine
from .models import ReflectRequest, ReflectResponse
from .settings import Settings

log = logging.getLogger(__name__)

# _LOG_REQUESTS and _STRIP_KEYS are now read from Settings (via Settings.from_env)
# so all config comes from a single place. See settings.py.


def _strip_tool_arg_keys(tool_calls: list[dict], keys: frozenset[str]) -> list[dict]:
    """Return a copy of tool_calls with the named argument keys removed."""
    result = []
    for tc in tool_calls:
        fn = tc.get("function") or {}
        if not isinstance(fn, dict):
            result.append(tc)
            continue
        raw_args = fn.get("arguments", "")
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            if isinstance(args, dict):
                args = {k: v for k, v in args.items() if k not in keys}
            new_args = json.dumps(args) if isinstance(args, dict) else raw_args
        except (json.JSONDecodeError, TypeError):
            new_args = raw_args
        result.append({**tc, "function": {**fn, "arguments": new_args}})
    return result


def create_app(engine: ReflectionEngine | None = None) -> FastAPI:
    """Build the FastAPI app. Inject ``engine`` in tests; defaults to env config."""
    settings = engine.settings if engine is not None else Settings.from_env()
    engine = engine or ReflectionEngine(settings)

    app = FastAPI(
        title="Rossoctl SPARC reflection service",
        version="0.1.0",
        summary="In-process SPARC pre-tool reflection over HTTP.",
    )
    app.state.engine = engine
    app.state.settings = settings

    @app.get("/healthz")
    def healthz() -> dict[str, object]:
        return {
            "status": "ok",
            "provider": settings.provider,
            "model": settings.model,
            "track": settings.track,
        }

    @app.get("/readyz")
    def readyz() -> dict[str, object]:
        ok, detail = engine.ready()
        if not ok:
            raise HTTPException(status_code=503, detail={"status": "not_ready", "reason": detail})
        return {"status": "ready", "provider": settings.provider, "model": settings.model}

    @app.post("/reflect", response_model=ReflectResponse)
    async def reflect(request: ReflectRequest) -> ReflectResponse:
        if settings.log_requests:
            log.info("incoming reflect request: %s", request.model_dump_json())

        if settings.strip_tool_arg_keys and request.tool_calls:
            request = request.model_copy(
                update={"tool_calls": _strip_tool_arg_keys(request.tool_calls, settings.strip_tool_arg_keys)}
            )
            if settings.log_requests:
                log.info("after strip (%s): tool_calls=%s", sorted(settings.strip_tool_arg_keys), request.tool_calls)

        # SPARCReflectionComponent.process is synchronous (and CPU/IO bound on the
        # LLM call); run it off the event loop so the service stays responsive.
        try:
            return await run_in_threadpool(engine.reflect, request)
        except ValueError as exc:  # bad input (e.g. unsupported track) → 400
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        except Exception:
            # Reflection failure → 502 so the plugin can apply its fail policy.
            # Keep the full error in the service logs; never echo provider
            # exception text to the caller — it can embed endpoints/credentials.
            log.exception("reflection failed")
            raise HTTPException(status_code=502, detail={"error": "reflection failed"}) from None

    return app


app = create_app()
