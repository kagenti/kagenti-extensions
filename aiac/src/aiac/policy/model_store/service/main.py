import logging
import os
import sqlite3
import threading
from contextlib import asynccontextmanager
from typing import Annotated

import uvicorn
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import JSONResponse, Response

from aiac.policy.model.models import ServicePolicyModel
from aiac.policy.model_store.keying import decode_service_id

logger = logging.getLogger(__name__)

DB_PATH = os.getenv("SERVICEPOLICY_DB_PATH", "/data/policy_model.db")

# Returned to the client on any SQLite failure. The concrete exception (which can carry
# schema/path internals) is logged server-side instead of echoed in the response body —
# never expose stack-trace / driver detail to an external caller.
_DB_ERROR_BODY = {"error": "database error"}

# In-memory cache of ServicePolicyModel rows keyed by service_id — the authoritative
# serving layer. All reads are served from here; SQLite is the durable write-through backend.
_cache: dict[str, ServicePolicyModel] = {}
_db_conn: sqlite3.Connection | None = None

# Serializes the read-modify-write of the cache + SQLite backend across the mutating
# endpoints. The cache dict and the shared connection are mutated by every request thread,
# so without this a concurrent create/update/delete/clear can interleave and leave the cache
# out of sync with the durable rows. Held only around the local SQLite write + cache mutation
# (never across network I/O).
_write_lock = threading.Lock()


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
    # Return every cached SPM referencing the given role id across BOTH inbound rule lists
    # (allow and deny) — a role that appears only in a deny edge must still surface. This must
    # be answered from the store (not the IdP): the SPM is the source of truth, so stale
    # role->service mappings the live IdP no longer reflects still show up here — which is
    # exactly what override-purge needs. Never 404s; empty list on no match.
    return [
        spm
        for spm in _cache.values()
        if any(rule.role.id == role for rule in (*spm.inbound_allow_rules, *spm.inbound_deny_rules))
    ]


@app.delete("/policy/services", response_model=None)
def clear_service_policies(
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
) -> Response:
    # Drop every SPM — durable row and cache entry alike. Distinct path from the by-id
    # DELETE (``/policy/services`` vs ``/policy/services/{service_id}``), so FastAPI routes
    # unambiguously. Intended for test harnesses that need a clean slate: without it the
    # StatefulSet PV outlives image redeploys, so pre-fix cruft accumulates across runs
    # (override=False appends). Never 404s; clearing an already-empty store is a no-op 204.
    with _write_lock:
        try:
            conn.execute("DELETE FROM service_policies")
        except sqlite3.Error:
            logger.exception("clear_service_policies: SQLite error")
            return JSONResponse(status_code=502, content=_DB_ERROR_BODY)
        _cache.clear()
    return Response(status_code=204)


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
    if body.service_id != service_id:
        # The body's own service_id must agree with the path; a mismatch would silently
        # persist a row under the wrong key. Fail loud with a 422 instead.
        raise HTTPException(
            status_code=422,
            detail=f"body.service_id {body.service_id!r} does not match path service_id {service_id!r}",
        )
    with _write_lock:
        try:
            conn.execute(
                "INSERT OR REPLACE INTO service_policies (service_id, spec) VALUES (?, ?)",
                (service_id, body.model_dump_json()),
            )
        except sqlite3.Error:
            logger.exception("upsert_service_policy: SQLite error")
            return JSONResponse(status_code=502, content=_DB_ERROR_BODY)
        _cache[service_id] = body
    return Response(status_code=204)


@app.delete("/policy/services/{service_id}", response_model=None)
def delete_service_policy(
    service_id: str,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
) -> Response:
    service_id = decode_service_id(service_id)
    with _write_lock:
        try:
            conn.execute("DELETE FROM service_policies WHERE service_id = ?", (service_id,))
        except sqlite3.Error:
            logger.exception("delete_service_policy: SQLite error")
            return JSONResponse(status_code=502, content=_DB_ERROR_BODY)
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
