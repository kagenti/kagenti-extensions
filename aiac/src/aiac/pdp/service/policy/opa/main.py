"""PDP Policy Writer (OPA) — filesystem stub.

Generates Rego packages via ``rego.py`` and writes them as ``.rego`` files to
a configurable output directory. No Kubernetes client — the ``rego.py`` module
is shared with the final (1.13) implementation.
"""

import os
from pathlib import Path

from fastapi import Depends, FastAPI
from starlette.responses import JSONResponse, Response

from aiac.pdp.service.policy.opa.rego import (
    generate_inbound_rego,
    generate_outbound_rego,
    slugify,
)
from aiac.policy.model.models import AgentPolicyModel, PolicyModel


def get_output_dir() -> Path:
    return Path(os.environ.get("REGO_OUTPUT_DIR", "/rego"))


def _upsert_agent(out_dir: Path, model: AgentPolicyModel) -> None:
    slug = slugify(model.agent_id)
    (out_dir / f"{slug}.inbound.rego").write_text(generate_inbound_rego(model))
    (out_dir / f"{slug}.outbound.rego").write_text(generate_outbound_rego(model))


def _delete_agent(out_dir: Path, agent_id: str) -> None:
    slug = slugify(agent_id)
    (out_dir / f"{slug}.inbound.rego").unlink(missing_ok=True)
    (out_dir / f"{slug}.outbound.rego").unlink(missing_ok=True)


def _delete_all(out_dir: Path) -> None:
    for rego_file in out_dir.glob("*.rego"):
        rego_file.unlink(missing_ok=True)


def _run_write(op) -> Response:
    """Run a filesystem write op, mapping any OSError to a 502 response."""
    try:
        op()
        return Response(status_code=204)
    except OSError as e:
        return JSONResponse(status_code=502, content={"error": str(e)})


app = FastAPI()


@app.post("/policy", status_code=204)
def upsert_policy(
    policy: PolicyModel,
    out_dir: Path = Depends(get_output_dir),
):
    def _op():
        for agent in policy.agents:
            _upsert_agent(out_dir, agent)

    return _run_write(_op)


@app.post("/policy/agents/{agent_id}", status_code=204)
def upsert_agent(
    agent_id: str,
    model: AgentPolicyModel,
    out_dir: Path = Depends(get_output_dir),
):
    return _run_write(lambda: _upsert_agent(out_dir, model))


@app.delete("/policy/agents/{agent_id}", status_code=204)
def delete_agent(
    agent_id: str,
    out_dir: Path = Depends(get_output_dir),
):
    return _run_write(lambda: _delete_agent(out_dir, agent_id))


@app.delete("/policy", status_code=204)
def delete_all(out_dir: Path = Depends(get_output_dir)):
    return _run_write(lambda: _delete_all(out_dir))


@app.get("/health")
def health(out_dir: Path = Depends(get_output_dir)):
    if out_dir.is_dir() and os.access(out_dir, os.W_OK):
        return {"status": "ok"}
    return JSONResponse(
        status_code=503,
        content={"status": "unavailable", "error": f"{out_dir} is not a writable directory"},
    )
