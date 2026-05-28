import os
from pathlib import Path

import requests
from dotenv import load_dotenv

from .models import User, RealmRole, Client, ClientScope, ClientRole, RoleMappings

load_dotenv(Path(__file__).resolve().parent / ".env")


def _base_url() -> str:
    return os.getenv("AC_SERVICE_URL", "http://127.0.0.1:7070")


def _params(realm: str) -> dict[str, str]:
    return {"realm": realm}


def _check(resp) -> None:
    if not resp.ok:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text}")


def get_users(realm: str) -> list[User]:
    resp = requests.get(f"{_base_url()}/users", params=_params(realm))
    _check(resp)
    return [User.model_validate(u) for u in resp.json()]


def get_realm_roles(realm: str) -> list[RealmRole]:
    resp = requests.get(f"{_base_url()}/realm-roles", params=_params(realm))
    _check(resp)
    return [RealmRole.model_validate(r) for r in resp.json()]


def get_clients(realm: str) -> list[Client]:
    resp = requests.get(f"{_base_url()}/clients", params=_params(realm))
    _check(resp)
    return [Client.model_validate(c) for c in resp.json()]


def get_client_scopes(realm: str) -> list[ClientScope]:
    resp = requests.get(f"{_base_url()}/client-scopes", params=_params(realm))
    _check(resp)
    return [ClientScope.model_validate(s) for s in resp.json()]


def get_user_role_mappings(user_id: str, realm: str) -> RoleMappings:
    resp = requests.get(f"{_base_url()}/users/{user_id}/role-mappings", params=_params(realm))
    _check(resp)
    return RoleMappings.model_validate(resp.json())


def get_client_roles(client_id: str, realm: str) -> list[ClientRole]:
    resp = requests.get(f"{_base_url()}/clients/{client_id}/roles", params=_params(realm))
    _check(resp)
    return [ClientRole.model_validate(r) for r in resp.json()]


def assign_client_roles(
    user_id: str, client_id: str, roles: list[ClientRole], realm: str
) -> None:
    url = f"{_base_url()}/users/{user_id}/role-mappings/clients/{client_id}"
    resp = requests.post(url, json=[r.model_dump() for r in roles], params=_params(realm))
    _check(resp)


def revoke_client_roles(
    user_id: str, client_id: str, roles: list[ClientRole], realm: str
) -> None:
    url = f"{_base_url()}/users/{user_id}/role-mappings/clients/{client_id}"
    resp = requests.delete(url, json=[r.model_dump() for r in roles], params=_params(realm))
    _check(resp)
