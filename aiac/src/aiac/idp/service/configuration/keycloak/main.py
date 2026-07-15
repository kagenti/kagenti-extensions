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


class _InvariantViolation(Exception):
    """Raised when Keycloak facts violate an AIAC assumption the IdP boundary must hold
    (e.g. Assumption 1 — no cross-kind role). Surfaced as HTTP 409, distinct from the 502
    used for upstream KeycloakError, so callers can tell an invariant breach from an outage."""


def _assert_single_kind(members: list[dict], role_name: str) -> None:
    """Assumption 1 (no cross-kind role): a role must be held by human users *or* by agent
    service accounts, never both — otherwise a single ``actorIds`` list cannot represent it.
    Keycloak names an agent service account ``service-account-<clientId>``."""
    has_agent = any(m.get("username", "").startswith("service-account-") for m in members)
    has_user = any(not m.get("username", "").startswith("service-account-") for m in members)
    if has_agent and has_user:
        raise _InvariantViolation(
            f"role '{role_name}' is held by both human users and agent service accounts "
            f"(Assumption 1: no cross-kind role)"
        )


def _assert_single_owner(admin: "KeycloakAdmin", scope: dict) -> None:
    """Assumption 2 (single scope owner): an AIAC-managed client scope must be exposed as a
    default scope by exactly one client. Keycloak client scopes are realm-level and assignable
    to many clients, so this is not a Keycloak guarantee — detect a multi-owner scope by scanning
    clients' default scopes and fail loud, since a single ``Scope.serviceId`` cannot represent it."""
    scope_id = scope["id"]
    owners = [
        client
        for client in admin.get_clients()
        if any(s["id"] == scope_id for s in admin.get_client_default_client_scopes(client["id"]))
    ]
    if len(owners) > 1:
        raise _InvariantViolation(
            f"AIAC-managed scope '{scope.get('name', scope_id)}' is exposed by {len(owners)} "
            f"clients (Assumption 2: a scope has exactly one owner)"
        )


def _is_aiac_managed(attributes: dict | None) -> bool:
    """True when a Keycloak role/scope carries the ``aiac.managed`` provisioning marker.
    Realm-role attribute values are lists of strings; client-scope values are plain strings —
    tolerate both (mirrors ``_is_aiac_managed`` in ``aiac.idp.configuration.models``)."""
    value = (attributes or {}).get(_AIAC_MANAGED_ATTRIBUTE)
    if isinstance(value, list):
        return "true" in value
    return value == "true"


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
        client = admin.get_client(service_id)
        service_client_id = client["clientId"]
        roles: list[dict] = []

        # Client roles on the agent's own client (original path): each carries
        # clientRole=true and containerId, used to classify + resolve owner.
        client_roles = admin.get_client_roles(service_id)
        owners: dict[str, str] = {}
        for role in client_roles:
            container_id = role.get("containerId") or service_id
            if container_id not in owners:
                owners[container_id] = admin.get_client(container_id)["clientId"]
            role["kind"] = "Agent"
            role["actorIds"] = [owners[container_id]]
        roles.extend(client_roles)

        # Realm roles assigned to the service account (provisioning path used by the
        # Configuration library). These are aiac-managed realm roles mapped to the service
        # via assign_realm_roles on the service-account user. Classify them as Agent roles
        # owned by this service, so the PCE sees them as outbound-capable roles.
        # NOTE: get_realm_roles_of_user returns role stubs without attributes, so re-fetch
        # each role's full representation to check the aiac.managed marker.
        try:
            sa_user = admin.get_client_service_account_user(service_id)
            sa_realm_roles = admin.get_realm_roles_of_user(sa_user["id"])
            seen_ids = {r["id"] for r in client_roles}
            for stub in sa_realm_roles:
                if stub["id"] in seen_ids:
                    continue
                full_role = admin.get_realm_role_by_id(stub["id"])
                if _is_aiac_managed(full_role.get("attributes")):
                    full_role["kind"] = "Agent"
                    full_role["actorIds"] = [service_client_id]
                    roles.append(full_role)
        except KeycloakError:
            pass  # service has no service account — skip

        return roles
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
        scopes = admin.get_client_default_client_scopes(service_id)
        if scopes:
            # Scope.serviceId = the owning client. For a per-service listing the owner is this
            # service; resolve its serviceId (clientId). AIAC-managed scopes must have exactly
            # one owner (Assumption 2) — enforce fail-loud. Built-ins are shared by design, so
            # they are exempt from the single-owner scan.
            owner = admin.get_client(service_id)["clientId"]
            for scope in scopes:
                if _is_aiac_managed(scope.get("attributes")):
                    _assert_single_owner(admin, scope)  # Assumption 2, fail loud
                scope["serviceId"] = owner
        return scopes
    except _InvariantViolation as e:
        return JSONResponse(status_code=409, content={"error": str(e)})
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
        roles = admin.get_realm_roles(brief_representation=False)
        for role in roles:
            # Realm roles are user roles (clientRole == false) -> kind=User (Assumption 3).
            role["kind"] = "User"
            # For AIAC-managed user roles, actorIds = the member usernames — the same values
            # GET /subjects?role_id= returns for this role (SPM/APM alignment, 1.12). Built-ins
            # are left unenriched (no member scan).
            if _is_aiac_managed(role.get("attributes")):
                members = admin.get_realm_role_members(role["name"])
                _assert_single_kind(members, role["name"])  # Assumption 1, fail loud
                role["actorIds"] = [m["username"] for m in members]
        return roles
    except _InvariantViolation as e:
        return JSONResponse(status_code=409, content={"error": str(e)})
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
