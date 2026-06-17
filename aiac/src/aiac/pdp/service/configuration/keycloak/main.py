import os
from contextlib import asynccontextmanager
from pathlib import Path
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Query
from keycloak import KeycloakAdmin
from keycloak.exceptions import KeycloakError
from pydantic import BaseModel
from typing import Literal
from starlette.responses import JSONResponse

_admin: KeycloakAdmin | None = None


def get_admin(realm: str | None = Query(None)) -> KeycloakAdmin:
    if realm is None:
        return _admin
    return KeycloakAdmin(
        server_url=os.environ["KEYCLOAK_URL"],
        realm_name=realm,
        username=os.environ["KEYCLOAK_ADMIN_USERNAME"],
        password=os.environ["KEYCLOAK_ADMIN_PASSWORD"],
    )


@asynccontextmanager
async def _lifespan(application: FastAPI):
    global _admin
    load_dotenv(Path(__file__).parent / ".env")
    _admin = KeycloakAdmin(
        server_url=os.environ["KEYCLOAK_URL"],
        realm_name=os.environ["KEYCLOAK_REALM"],
        username=os.environ["KEYCLOAK_ADMIN_USERNAME"],
        password=os.environ["KEYCLOAK_ADMIN_PASSWORD"],
    )
    yield


app = FastAPI(lifespan=_lifespan)


class _ScopeCreate(BaseModel):
    name: str
    description: str = ""


@app.get("/subjects")
def list_subjects(admin: KeycloakAdmin = Depends(get_admin)):
    try:
        return admin.get_users()
    except KeycloakError as e:
        return JSONResponse(status_code=502, content={"error": str(e)})


@app.get("/roles")
def list_roles(admin: KeycloakAdmin = Depends(get_admin)):
    try:
        return admin.get_realm_roles()
    except KeycloakError as e:
        return JSONResponse(status_code=502, content={"error": str(e)})


@app.get("/services")
def list_services(admin: KeycloakAdmin = Depends(get_admin)):
    try:
        return admin.get_clients()
    except KeycloakError as e:
        return JSONResponse(status_code=502, content={"error": str(e)})


@app.get("/scopes")
def list_scopes(admin: KeycloakAdmin = Depends(get_admin)):
    try:
        return admin.get_client_scopes()
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


@app.get("/services/{service_id}/roles")
def list_service_roles(service_id: str, admin: KeycloakAdmin = Depends(get_admin)):
    try:
        return admin.get_realm_roles_of_client_scope(service_id)
    except KeycloakError as e:
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
        scope_id = admin.create_client_scope(
            {"name": body.name, "description": body.description, "protocol": "openid-connect"}
        )
        admin.add_default_default_client_scope(service_id, scope_id)
        return admin.get_client_scope(scope_id)
    except KeycloakError as e:
        return JSONResponse(status_code=502, content={"error": str(e)})


@app.get("/services/{service_id}")
def get_service(service_id: str, admin: KeycloakAdmin = Depends(get_admin)):
    try:
        return admin.get_client(service_id)
    except KeycloakError as e:
        return JSONResponse(status_code=502, content={"error": str(e)})


class _ServicePatch(BaseModel):
    type: Literal["Agent", "Tool"]


@app.patch("/services/{service_id}", status_code=200)
def patch_service(service_id: str, body: _ServicePatch, admin: KeycloakAdmin = Depends(get_admin)):
    try:
        client = admin.get_client(service_id)
        existing_attrs = client.get("attributes") or {}
        admin.update_client(service_id, {"attributes": {**existing_attrs, "kagenti.service.type": body.type}})
        return admin.get_client(service_id)
    except KeycloakError as e:
        return JSONResponse(status_code=502, content={"error": str(e)})


class _RoleCreate(BaseModel):
    name: str
    description: str = ""


@app.post("/scopes", status_code=201)
def create_scope_standalone(body: _ScopeCreate, admin: KeycloakAdmin = Depends(get_admin)):
    try:
        scope_id = admin.create_client_scope(
            {"name": body.name, "description": body.description, "protocol": "openid-connect"}
        )
        return admin.get_client_scope(scope_id)
    except KeycloakError as e:
        if e.response_code == 409:
            return JSONResponse(status_code=409, content={"error": str(e)})
        return JSONResponse(status_code=502, content={"error": str(e)})


@app.post("/services/{service_id}/scopes/{scope_id}", status_code=201)
def assign_scope_to_service(service_id: str, scope_id: str, admin: KeycloakAdmin = Depends(get_admin)):
    try:
        admin.add_default_default_client_scope(service_id, scope_id)
        return JSONResponse(status_code=201, content={})
    except KeycloakError as e:
        if e.response_code == 409:
            return JSONResponse(status_code=409, content={"error": str(e)})
        return JSONResponse(status_code=502, content={"error": str(e)})


@app.post("/roles", status_code=201)
def create_role(body: _RoleCreate, admin: KeycloakAdmin = Depends(get_admin)):
    try:
        admin.create_realm_role({"name": body.name, "description": body.description})
        return admin.get_realm_role(body.name)
    except KeycloakError as e:
        if e.response_code == 409:
            return JSONResponse(status_code=409, content={"error": str(e)})
        return JSONResponse(status_code=502, content={"error": str(e)})


@app.post("/services/{service_id}/roles/{role_id}", status_code=201)
def assign_role_to_service(service_id: str, role_id: str, admin: KeycloakAdmin = Depends(get_admin)):
    try:
        admin.assign_realm_roles_to_client_scope(service_id, [{"id": role_id}])
        return JSONResponse(status_code=201, content={})
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


@app.get("/roles/{role_name}/scopes")
def list_role_scopes(role_name: str, admin: KeycloakAdmin = Depends(get_admin)):
    try:
        role = admin.get_realm_role(role_name)
        role_id = role["id"]
        mapped = []
        for scope in admin.get_client_scopes():
            all_mappings = admin.get_all_roles_of_client_scope(scope["id"])
            realm_mappings = all_mappings.get("realmMappings", [])
            if any(r["id"] == role_id for r in realm_mappings):
                mapped.append(scope)
        return mapped
    except KeycloakError as e:
        return JSONResponse(status_code=502, content={"error": str(e)})


@app.get("/health")
def health(admin: KeycloakAdmin = Depends(get_admin)):
    try:
        admin.get_server_info()
        return {"status": "ok"}
    except KeycloakError as e:
        return JSONResponse(status_code=503, content={"status": "unavailable", "error": str(e)})
