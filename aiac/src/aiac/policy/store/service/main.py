import os
import sqlite3
from contextlib import asynccontextmanager
from typing import Annotated

import uvicorn
from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse, Response

from aiac.policy.model.models import AgentPolicyModel, PolicyModel

DB_PATH = os.getenv("AGENTPOLICY_DB_PATH", "/data/policy_model.db")

_cache: dict[str, AgentPolicyModel] = {}
_db_conn: sqlite3.Connection | None = None


def get_db() -> sqlite3.Connection:
    assert _db_conn is not None
    return _db_conn


def _init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS agent_policies "
        "(agent_id TEXT PRIMARY KEY, spec TEXT NOT NULL)"
    )


def _load_cache(conn: sqlite3.Connection) -> None:
    global _cache
    rows = conn.execute("SELECT agent_id, spec FROM agent_policies").fetchall()
    _cache = {
        agent_id: AgentPolicyModel.model_validate_json(spec)
        for agent_id, spec in rows
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _db_conn
    _db_conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
    _init_db(_db_conn)
    _load_cache(_db_conn)
    yield
    if _db_conn:
        _db_conn.close()
        _db_conn = None


app = FastAPI(lifespan=lifespan)


@app.get("/policy", response_model=None)
def get_policy() -> PolicyModel:
    return PolicyModel(agents=list(_cache.values()))


@app.get("/policy/agents/{agent_id}", response_model=None)
def get_agent_policy(agent_id: str):
    if agent_id not in _cache:
        return JSONResponse(status_code=404, content={"error": f"agent {agent_id} not found"})
    return _cache[agent_id]


@app.post("/policy/agents/{agent_id}", response_model=None)
def upsert_agent_policy(
    agent_id: str,
    body: AgentPolicyModel,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
) -> Response:
    try:
        conn.execute(
            "INSERT OR REPLACE INTO agent_policies (agent_id, spec) VALUES (?, ?)",
            (agent_id, body.model_dump_json()),
        )
    except sqlite3.Error as e:
        return JSONResponse(status_code=502, content={"error": str(e)})
    _cache[agent_id] = body
    return Response(status_code=204)


@app.post("/policy", response_model=None)
def replace_policy(
    body: PolicyModel,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
) -> Response:
    try:
        conn.execute("BEGIN")
        conn.execute("DELETE FROM agent_policies")
        for agent in body.agents:
            conn.execute(
                "INSERT INTO agent_policies (agent_id, spec) VALUES (?, ?)",
                (agent.agent_id, agent.model_dump_json()),
            )
        conn.execute("COMMIT")
    except sqlite3.Error as e:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        return JSONResponse(status_code=502, content={"error": str(e)})
    global _cache
    _cache = {agent.agent_id: agent for agent in body.agents}
    return Response(status_code=204)


@app.delete("/policy/agents/{agent_id}", response_model=None)
def delete_agent_policy(
    agent_id: str,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
) -> Response:
    try:
        conn.execute(
            "DELETE FROM agent_policies WHERE agent_id = ?", (agent_id,)
        )
    except sqlite3.Error as e:
        return JSONResponse(status_code=502, content={"error": str(e)})
    _cache.pop(agent_id, None)
    return Response(status_code=204)


@app.delete("/policy", response_model=None)
def delete_all_policies(
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
) -> Response:
    try:
        conn.execute("DELETE FROM agent_policies")
    except sqlite3.Error as e:
        return JSONResponse(status_code=502, content={"error": str(e)})
    global _cache
    _cache = {}
    return Response(status_code=204)


@app.get("/health", response_model=None)
def health(conn: Annotated[sqlite3.Connection, Depends(get_db)]):
    try:
        conn.execute("SELECT 1")
        return {"status": "ok"}
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unavailable"})


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=7074)
