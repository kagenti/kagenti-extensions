import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Query
from keycloak import KeycloakAdmin
from keycloak.exceptions import KeycloakError
from pydantic import BaseModel
from starlette.responses import JSONResponse

_cache: dict[str, KeycloakAdmin] = {}
_lock = threading.Lock()

# AIAC naming convention: stamp every role/scope this service provisions with the
# ``aiac.managed=true`` Keycloak attribute so downstream consumers (the Policy Computation
# Engine) can keep only AIAC-provisioned entities and drop Keycloak built-ins. Defined locally
# because this service image ships only ``main.py`` (the aiac library is not on its path); it
# mirrors ``AIAC_MANAGED_ATTRIBUTE`` in ``aiac.idp.configuration.models``. Realm-role attribute
# values are lists of strings; client-scope attribute values are plain strings.
_AIAC_MANAGED_ATTRIBUTE = "aiac.managed"

# Keycloak client attribute carrying a service's type. AIAC calls the concept "service type"
# (``Agent``/``Tool``); the Keycloak attribute is named ``client.type`` and its value is a plain
# string. Mirrors ``SERVICE_TYPE_ATTRIBUTE`` in ``aiac.idp.configuration.models`` (the aiac library
# is not on this service image's path, so it is redefined locally).
_SERVICE_TYPE_ATTRIBUTE = "client.type"


def _get_or_create_admin(realm: str) -> KeycloakAdmin:
    if realm not in _cache:
        with _lock:
            if realm not in _cache:
                _cache[realm] = KeycloakAdmin(
                    server_url=os.environ["KEYCLOAK_URL"],
                    realm_name=realm,
                    user_realm_name=os.environ["KEYCLOAK_ADMIN_REALM"],
                    username=os.environ["KEYCLOAK_ADMIN_USERNAME"],
                    password=os.environ["KEYCLOAK_ADMIN_PASSWORD"],
                )
    return _cache[realm]


def get_admin(realm: str = Query(...)) -> KeycloakAdmin:
    return _get_or_create_admin(realm)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    load_dotenv(Path(__file__).parent / ".env")
    yield


app = FastAPI(lifespan=_lifespan)


class _ScopeCreate(BaseModel):
    name: str
    description: str = ""


class _RoleCreate(BaseModel):
    name: str
    description: str = ""


class _ServiceTypeUpdate(BaseModel):
    type: Literal["Agent", "Tool"]


@app.get("/subjects")
def list_subjects(
    realm: str = Query(...),
    role_id: str | None = Query(default=None),
    admin: KeycloakAdmin = Depends(get_admin),
):
    try:
        if role_id is not None:
            role = admin.get_realm_role_by_id(role_id)
            role_name = role["name"]
            users = admin.get_realm_role_members(role_name)
            result = []
            for user in users:
                raw = admin.get_all_roles_of_user(user["id"])
                result.append({
                    **user,
                    "realmMappings": raw.get("realmMappings", []),
                    "serviceMappings": raw.get("clientMappings", {}),
                })
            return result
        return admin.get_users()
    except KeycloakError as e:
        return JSONResponse(status_code=502, content={"error": str(e)})


@app.get("/subjects/{subject_id}/assignments")
def get_subject_assignments(subject_id: str, admin: KeycloakAdmin = Depends(get_admin)):
    try:
        raw = admin.get_all_roles_of_user(subject_id)
        # Remap clientMappings → serviceMappings for PDP naming
        return {
            "realmMappings": raw.get("realmMappings", []),
            "serviceMappings": raw.get("clientMappings", {}),
        }
    except KeycloakError as e:
        return JSONResponse(status_code=502, content={"error": str(e)})


@app.get("/services")
def list_services(admin: KeycloakAdmin = Depends(get_admin)):
    try:
        return admin.get_clients()
    except KeycloakError as e:
        return JSONResponse(status_code=502, content={"error": str(e)})


@app.get("/services/{service_id}")
def get_service(service_id: str, admin: KeycloakAdmin = Depends(get_admin)):
    try:
        return admin.get_client(service_id)
    except KeycloakError as e:
        return JSONResponse(status_code=502, content={"error": str(e)})


@app.post("/services/{service_id}/type", status_code=200)
def set_service_type(
    service_id: str, body: _ServiceTypeUpdate, admin: KeycloakAdmin = Depends(get_admin)
):
    try:
        client = admin.get_client(service_id)
        # Merge into the existing attributes so we don't clobber other client attributes;
        # Keycloak replaces the whole attributes map on update.
        attributes = dict(client.get("attributes") or {})
        attributes[_SERVICE_TYPE_ATTRIBUTE] = body.type  # capitalized plain string
        admin.update_client(service_id, {"attributes": attributes})
        return admin.get_client(service_id)
    except KeycloakError as e:
        return JSONResponse(status_code=502, content={"error": str(e)})


@app.get("/services/{service_id}/roles")
def list_service_roles(service_id: str, admin: KeycloakAdmin = Depends(get_admin)):
    try:
        sa_user = admin.get_client_service_account_user(service_id)
        user_id = sa_user["id"]
        return admin.get_realm_roles_of_user(user_id)
    except KeycloakError as e:
        if e.response_code == 400:
            return []
        return JSONResponse(status_code=502, content={"error": str(e)})


@app.post("/services/{service_id}/roles/{role_id}", status_code=201)
def assign_role_to_service(service_id: str, role_id: str, admin: KeycloakAdmin = Depends(get_admin)):
    try:
        sa_user = admin.get_client_service_account_user(service_id)
        user_id = sa_user["id"]
        # Keycloak's role-mappings endpoint needs the full role representation (id + name),
        # not just the id, so resolve the role before assigning it to the service account.
        role = admin.get_realm_role_by_id(role_id)
        admin.assign_realm_roles(user_id, [role])
        return JSONResponse(status_code=201, content={})
    except KeycloakError as e:
        if e.response_code == 409:
            return JSONResponse(status_code=409, content={"error": str(e)})
        return JSONResponse(status_code=502, content={"error": str(e)})


@app.get("/services/{service_id}/scopes")
def list_service_scopes(service_id: str, admin: KeycloakAdmin = Depends(get_admin)):
    try:
        return admin.get_client_default_client_scopes(service_id)
    except KeycloakError as e:
        return JSONResponse(status_code=502, content={"error": str(e)})


@app.post("/services/{service_id}/scopes", status_code=201)
def create_scope(service_id: str, body: _ScopeCreate, admin: KeycloakAdmin = Depends(get_admin)):
    try:
        scope_id = admin.create_client_scope({
            "name": body.name,
            "description": body.description,
            "protocol": "openid-connect",
            "attributes": {_AIAC_MANAGED_ATTRIBUTE: "true"},  # AIAC provisioning marker
        })
        admin.add_client_default_client_scope(service_id, scope_id, {})
        return admin.get_client_scope(scope_id)
    except KeycloakError as e:
        return JSONResponse(status_code=502, content={"error": str(e)})


@app.post("/services/{service_id}/scopes/{scope_id}", status_code=201)
def assign_scope_to_service(service_id: str, scope_id: str, admin: KeycloakAdmin = Depends(get_admin)):
    try:
        admin.add_client_default_client_scope(service_id, scope_id, {})
        return JSONResponse(status_code=201, content={})
    except KeycloakError as e:
        if e.response_code == 409:
            return JSONResponse(status_code=409, content={"error": str(e)})
        return JSONResponse(status_code=502, content={"error": str(e)})


@app.get("/roles")
def list_roles(admin: KeycloakAdmin = Depends(get_admin)):
    try:
        # brief_representation=False so realm-role attributes (incl. the aiac.managed marker)
        # are returned; the brief representation Keycloak returns by default omits them.
        return admin.get_realm_roles(brief_representation=False)
    except KeycloakError as e:
        return JSONResponse(status_code=502, content={"error": str(e)})


@app.post("/roles", status_code=201)
def create_role(body: _RoleCreate, admin: KeycloakAdmin = Depends(get_admin)):
    try:
        admin.create_realm_role({
            "name": body.name,
            "description": body.description,
            "attributes": {_AIAC_MANAGED_ATTRIBUTE: ["true"]},  # AIAC provisioning marker
        })
        return admin.get_realm_role(body.name)
    except KeycloakError as e:
        if e.response_code == 409:
            return JSONResponse(status_code=409, content={"error": str(e)})
        return JSONResponse(status_code=502, content={"error": str(e)})


@app.get("/roles/{role_name}/composites")
def list_role_composites(role_name: str, admin: KeycloakAdmin = Depends(get_admin)):
    try:
        return admin.get_composite_realm_roles_of_role(role_name=role_name)
    except KeycloakError as e:
        return JSONResponse(status_code=502, content={"error": str(e)})


@app.get("/scopes")
def list_scopes(admin: KeycloakAdmin = Depends(get_admin)):
    try:
        return admin.get_client_scopes()
    except KeycloakError as e:
        return JSONResponse(status_code=502, content={"error": str(e)})


@app.post("/scopes", status_code=201)
def create_scope_standalone(body: _ScopeCreate, admin: KeycloakAdmin = Depends(get_admin)):
    try:
        scope_id = admin.create_client_scope({
            "name": body.name,
            "description": body.description,
            "protocol": "openid-connect",
            "attributes": {_AIAC_MANAGED_ATTRIBUTE: "true"},  # AIAC provisioning marker
        })
        return admin.get_client_scope(scope_id)
    except KeycloakError as e:
        if e.response_code == 409:
            return JSONResponse(status_code=409, content={"error": str(e)})
        return JSONResponse(status_code=502, content={"error": str(e)})


@app.get("/health")
def health():
    admin = _get_or_create_admin(os.environ["KEYCLOAK_ADMIN_REALM"])
    try:
        admin.get_server_info()
        return {"status": "ok"}
    except KeycloakError as e:
        return JSONResponse(status_code=503, content={"status": "unavailable", "error": str(e)})
