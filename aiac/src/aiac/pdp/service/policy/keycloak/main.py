import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import Body, Depends, FastAPI, Query
from keycloak import KeycloakAdmin
from keycloak.exceptions import KeycloakError
from starlette.responses import JSONResponse, Response

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


@app.post("/roles/{role_name}/composites", status_code=204)
def add_role_composites(
    role_name: str,
    roles: list[Any] = Body(...),
    admin: KeycloakAdmin = Depends(get_admin),
):
    try:
        admin.add_composite_realm_roles_to_role(role_name, roles)
        return Response(status_code=204)
    except KeycloakError as e:
        return JSONResponse(status_code=502, content={"error": str(e)})


@app.delete("/roles/{role_name}/composites", status_code=204)
def remove_role_composites(
    role_name: str,
    roles: list[Any] = Body(...),
    admin: KeycloakAdmin = Depends(get_admin),
):
    try:
        admin.remove_composite_realm_roles_to_role(role_name, roles)
        return Response(status_code=204)
    except KeycloakError as e:
        return JSONResponse(status_code=502, content={"error": str(e)})


@app.delete("/composites", status_code=204)
def clear_all_composites(admin: KeycloakAdmin = Depends(get_admin)):
    try:
        for role in admin.get_realm_roles():
            composites = admin.get_composite_realm_roles_of_role(role_name=role["name"])
            if composites:
                admin.remove_composite_realm_roles_to_role(role["name"], composites)
        return Response(status_code=204)
    except KeycloakError as e:
        return JSONResponse(status_code=502, content={"error": str(e)})


@app.post("/services/{service_id}/permissions", status_code=201)
def create_service_permission(
    service_id: str,
    body: dict[str, Any] = Body(...),
    admin: KeycloakAdmin = Depends(get_admin),
):
    try:
        created = admin.create_client_role(service_id, body)
        return JSONResponse(status_code=201, content=created)
    except KeycloakError as e:
        return JSONResponse(status_code=502, content={"error": str(e)})


@app.post("/services/{service_id}/scopes", status_code=201)
def create_service_scope(
    service_id: str,
    body: dict[str, Any] = Body(...),
    admin: KeycloakAdmin = Depends(get_admin),
):
    try:
        scope_payload = {
            "name": body["name"],
            "description": body.get("description", ""),
            "protocol": "openid-connect",
        }
        created = admin.create_client_scope(scope_payload)
        admin.add_default_default_client_scope(service_id, created["id"])
        return JSONResponse(status_code=201, content=created)
    except KeycloakError as e:
        return JSONResponse(status_code=502, content={"error": str(e)})


@app.get("/health")
def health(admin: KeycloakAdmin = Depends(get_admin)):
    try:
        admin.get_server_info()
        return {"status": "ok"}
    except KeycloakError as e:
        return JSONResponse(status_code=503, content={"status": "unavailable", "error": str(e)})
