import os
import sqlite3
from contextlib import asynccontextmanager
from typing import Annotated

import uvicorn
from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse, Response

from aiac.policy.model.models import ServicePolicyModel
from aiac.policy.store.keying import decode_service_id

DB_PATH = os.getenv("SERVICEPOLICY_DB_PATH", "/data/policy_model.db")

# In-memory cache of ServicePolicyModel rows keyed by service_id — the authoritative
# serving layer. All reads are served from here; SQLite is the durable write-through backend.
_cache: dict[str, ServicePolicyModel] = {}
_db_conn: sqlite3.Connection | None = None


def get_db() -> sqlite3.Connection:
    assert _db_conn is not None
    return _db_conn


def _init_db(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS service_policies (service_id TEXT PRIMARY KEY, spec TEXT NOT NULL)")


def _load_cache(conn: sqlite3.Connection) -> None:
    global _cache
    rows = conn.execute("SELECT service_id, spec FROM service_policies").fetchall()
    _cache = {service_id: ServicePolicyModel.model_validate_json(spec) for service_id, spec in rows}


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


@app.get("/policy/services", response_model=None)
def list_service_policies_by_role(role: str) -> list[ServicePolicyModel]:
    # Return every cached SPM whose inbound_rules reference the given role id. This must
    # be answered from the store (not the IdP): the SPM is the source of truth, so stale
    # role->service mappings the live IdP no longer reflects still show up here — which is
    # exactly what override-purge needs. Never 404s; empty list on no match.
    return [spm for spm in _cache.values() if any(rule.role.id == role for rule in spm.inbound_rules)]


@app.get("/policy/services/{service_id}", response_model=None)
def get_service_policy(service_id: str):
    service_id = decode_service_id(service_id)
    if service_id not in _cache:
        return JSONResponse(status_code=404, content={"error": f"service {service_id} not found"})
    return _cache[service_id]


@app.post("/policy/services/{service_id}", response_model=None)
def upsert_service_policy(
    service_id: str,
    body: ServicePolicyModel,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
) -> Response:
    service_id = decode_service_id(service_id)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO service_policies (service_id, spec) VALUES (?, ?)",
            (service_id, body.model_dump_json()),
        )
    except sqlite3.Error as e:
        return JSONResponse(status_code=502, content={"error": str(e)})
    _cache[service_id] = body
    return Response(status_code=204)


@app.delete("/policy/services/{service_id}", response_model=None)
def delete_service_policy(
    service_id: str,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
) -> Response:
    service_id = decode_service_id(service_id)
    try:
        conn.execute("DELETE FROM service_policies WHERE service_id = ?", (service_id,))
    except sqlite3.Error as e:
        return JSONResponse(status_code=502, content={"error": str(e)})
    _cache.pop(service_id, None)
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
