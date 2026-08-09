"""AIAC Agent Controller — FastAPI app factory + the four ``/apply/*`` routes.

The Controller is stateless. Each route dispatches to its use-case handler
(orchestrator or sub-agent), receives the ``(list[PolicyRule], override)`` tuple
the handler returns, and makes the **single** ``compute_and_apply(rules, override)``
call to the Policy Computation Engine. No per-use-case business logic, retry
handling, or state assembly lives here.

Responses are bare HTTP status codes: ``200 OK`` on success (no body). Upstream
failures are raised as FastAPI ``HTTPException``s by the handlers; the status
code is authoritative (the accompanying default JSON error body is incidental).
"""

import uvicorn
from fastapi import FastAPI
from fastapi.responses import Response

from aiac.agent.uc.offboarding.offboard import offboard_service
from aiac.agent.uc.onboarding.orchestrator import onboard_service
from aiac.agent.uc.policy_update.build import build_policy
from aiac.agent.uc.policy_update.rebuild import rebuild_policy
from aiac.agent.uc.role_update.role import update_role
from aiac.policy.computation import compute_and_apply, decommission

app = FastAPI()


@app.get("/health")
def health() -> dict[str, str]:
    # The Controller is stateless — it holds no local state and opens no
    # connection at rest — so /health is a bare liveness/readiness signal:
    # if the process is accepting requests it is ready. Upstream reachability
    # (IdP, PCE, NATS) is validated per-request by the handlers, not here.
    return {"status": "ok"}


@app.post("/apply/service/{service_id}")
def apply_service(service_id: str) -> Response:
    rules, override = onboard_service(service_id)
    compute_and_apply(rules, override)
    return Response(status_code=200)


@app.post("/apply/policy/build")
def apply_policy_build() -> Response:
    rules, override = build_policy()
    compute_and_apply(rules, override)
    return Response(status_code=200)


@app.post("/apply/policy/rebuild")
def apply_policy_rebuild() -> Response:
    rules, override = rebuild_policy()
    compute_and_apply(rules, override)
    return Response(status_code=200)


@app.post("/apply/role/{role_id}")
def apply_role(role_id: str) -> Response:
    rules, override = update_role(role_id)
    compute_and_apply(rules, override)
    return Response(status_code=200)


# Offboard is keyed by the clientId (the SPM key), NOT the Keycloak internal UUID that
# /apply/service/{service_id} carries: an offboarded client is gone from get_services(), so
# UUID→clientId resolution is impossible. The {service_id:path} converter carries slash-bearing
# SPIFFE-URI clientIds. Decommission is a whole-service teardown, so — BY DESIGN — it does NOT
# go through the (rules, override) → compute_and_apply path the other /apply/* routes share.
# compute_and_apply folds *incremental* rule updates into the SPM store; decommission is an
# authoritative offboard that must tear down a service's entire policy footprint (its SPM, its
# outbound edges on other SPMs, and its APM), which is not expressible as a rule delta. So this
# route intentionally calls the PCE's decommission() directly. See the implementation plan.
@app.post("/apply/offboard/{service_id:path}")
def apply_offboard(service_id: str) -> Response:
    decommission(offboard_service(service_id))
    return Response(status_code=200)


def main() -> None:
    uvicorn.run(app, host="0.0.0.0", port=7070)


if __name__ == "__main__":
    main()
