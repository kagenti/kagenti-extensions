import os
from contextlib import asynccontextmanager
from pathlib import Path
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Query
from keycloak import KeycloakAdmin
from keycloak.exceptions import KeycloakError
from pydantic import BaseModel
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


@app.get("/services/{service_id}/permissions")
def list_service_permissions(service_id: str, admin: KeycloakAdmin = Depends(get_admin)):
    try:
        return admin.get_client_roles(service_id)
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


@app.get("/roles/{role_name}/composites")
def list_role_composites(role_name: str, admin: KeycloakAdmin = Depends(get_admin)):
    try:
        return admin.get_composite_realm_roles_of_role(role_name=role_name)
    except KeycloakError as e:
        return JSONResponse(status_code=502, content={"error": str(e)})


@app.get("/health")
def health(admin: KeycloakAdmin = Depends(get_admin)):
    try:
        admin.get_server_info()
        return {"status": "ok"}
    except KeycloakError as e:
        return JSONResponse(status_code=503, content={"status": "unavailable", "error": str(e)})
