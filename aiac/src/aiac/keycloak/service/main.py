import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import Body, Depends, FastAPI, Query
from keycloak import KeycloakAdmin
from keycloak.exceptions import KeycloakError
from starlette.responses import JSONResponse, Response

def get_admin(realm: str = Query(...)) -> KeycloakAdmin:
    return KeycloakAdmin(
        server_url=os.environ["KEYCLOAK_URL"],
        realm_name=realm,
        username=os.environ["KEYCLOAK_ADMIN_USERNAME"],
        password=os.environ["KEYCLOAK_ADMIN_PASSWORD"],
    )


@asynccontextmanager
async def _lifespan(application: FastAPI):
    load_dotenv(Path(__file__).parent / ".env")
    yield


app = FastAPI(lifespan=_lifespan)


@app.get("/users")
def list_users(admin: KeycloakAdmin = Depends(get_admin)):
    try:
        return admin.get_users()
    except KeycloakError as e:
        return JSONResponse(status_code=502, content={"error": str(e)})


@app.get("/realm-roles")
def list_realm_roles(admin: KeycloakAdmin = Depends(get_admin)):
    try:
        return admin.get_realm_roles()
    except KeycloakError as e:
        return JSONResponse(status_code=502, content={"error": str(e)})


@app.get("/clients")
def list_clients(admin: KeycloakAdmin = Depends(get_admin)):
    try:
        return admin.get_clients()
    except KeycloakError as e:
        return JSONResponse(status_code=502, content={"error": str(e)})


@app.get("/client-scopes")
def list_client_scopes(admin: KeycloakAdmin = Depends(get_admin)):
    try:
        return admin.get_client_scopes()
    except KeycloakError as e:
        return JSONResponse(status_code=502, content={"error": str(e)})


@app.get("/users/{user_id}/role-mappings")
def get_user_role_mappings(user_id: str, admin: KeycloakAdmin = Depends(get_admin)):
    try:
        return admin.get_all_roles_of_user(user_id)
    except KeycloakError as e:
        return JSONResponse(status_code=502, content={"error": str(e)})


@app.get("/clients/{client_id}/roles")
def list_client_roles(client_id: str, admin: KeycloakAdmin = Depends(get_admin)):
    try:
        return admin.get_client_roles(client_id)
    except KeycloakError as e:
        return JSONResponse(status_code=502, content={"error": str(e)})


@app.post("/users/{user_id}/role-mappings/clients/{client_id}", status_code=204)
def assign_client_roles(
    user_id: str,
    client_id: str,
    roles: list[Any] = Body(...),
    admin: KeycloakAdmin = Depends(get_admin),
):
    try:
        admin.assign_client_role(user_id, client_id, roles)
        return Response(status_code=204)
    except KeycloakError as e:
        return JSONResponse(status_code=502, content={"error": str(e)})


@app.delete("/users/{user_id}/role-mappings/clients/{client_id}", status_code=204)
def delete_client_roles(
    user_id: str,
    client_id: str,
    roles: list[Any] = Body(...),
    admin: KeycloakAdmin = Depends(get_admin),
):
    try:
        admin.delete_client_roles_of_user(user_id, client_id, roles)
        return Response(status_code=204)
    except KeycloakError as e:
        return JSONResponse(status_code=502, content={"error": str(e)})
