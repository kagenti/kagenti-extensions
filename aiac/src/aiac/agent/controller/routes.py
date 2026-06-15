"""FastAPI controller — /apply/* route handlers and NATS consumer lifespan."""

import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from aiac.agent.controller.nats_consumer import _nats_consumer_loop
from aiac.agent.roles.orchestrator import dispatch as roles_dispatch
from aiac.agent.shared.state import BaseAgentState, TriggerContext


def _build_state(trigger_type: str, entity_id: str | None) -> BaseAgentState:
    return {
        "trigger": TriggerContext(trigger_type=trigger_type, entity_id=entity_id),
        "realm": os.getenv("AIAC_REALM", "master"),
        "policy_chunks": [],
        "domain_knowledge_chunks": [],
        "pdp_snapshot": None,
        "proposed_diff": None,
        "validation_errors": [],
        "added": [],
        "removed": [],
        "summary": "",
    }


async def _handle_policy_build() -> dict:
    # Policy Update Orchestrator — stub
    return {"status": "accepted", "trigger": "build"}


async def _handle_policy_rebuild() -> dict:
    # Policy Update Orchestrator — stub
    return {"status": "accepted", "trigger": "rebuild"}


async def _handle_role(role_id: str) -> dict:
    state = _build_state(f"role/{role_id}", role_id)
    result = roles_dispatch(state)
    return {
        "summary": result.get("summary", ""),
        "added": len(result.get("added", [])),
        "removed": len(result.get("removed", [])),
        "errors": len(result.get("validation_errors", [])),
    }


async def _handle_service(service_id: str) -> dict:
    # Service Onboarding Orchestrator — stub
    return {"status": "accepted", "trigger": f"service/{service_id}"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(
        _nats_consumer_loop(
            policy_handler=_handle_policy_build,
            role_handler=_handle_role,
            service_handler=_handle_service,
        )
    )
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(lifespan=lifespan)


@app.post("/apply/policy/build")
async def apply_policy_build() -> dict:
    return await _handle_policy_build()


@app.post("/apply/policy/rebuild")
async def apply_policy_rebuild() -> dict:
    return await _handle_policy_rebuild()


@app.post("/apply/role/{role_id}")
async def apply_role(role_id: str) -> dict:
    return await _handle_role(role_id)


@app.post("/apply/service/{service_id}")
async def apply_service(service_id: str) -> dict:
    return await _handle_service(service_id)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("aiac.agent.controller.routes:app", host="0.0.0.0", port=7070)
