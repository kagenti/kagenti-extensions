import os
from pathlib import Path

import requests
from dotenv import load_dotenv

from .models import Permission, Scope

load_dotenv(Path(__file__).resolve().parent / ".env")


def _base_url() -> str:
    return os.getenv("AIAC_PDP_POLICY_URL", "http://127.0.0.1:7073")


def _params(realm: str) -> dict[str, str]:
    return {"realm": realm}


def _check(resp) -> None:
    if not resp.ok:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text}")


def add_role_composites(role_name: str, permissions: list[Permission], realm: str) -> None:
    url = f"{_base_url()}/roles/{role_name}/composites"
    resp = requests.post(url, json=[p.model_dump() for p in permissions], params=_params(realm))
    _check(resp)


def remove_role_composites(role_name: str, permissions: list[Permission], realm: str) -> None:
    url = f"{_base_url()}/roles/{role_name}/composites"
    resp = requests.delete(url, json=[p.model_dump() for p in permissions], params=_params(realm))
    _check(resp)


def clear_all_composites(realm: str) -> None:
    resp = requests.delete(f"{_base_url()}/composites", params=_params(realm))
    _check(resp)


def create_service_permission(
    service_id: str, permission_name: str, description: str, realm: str
) -> Permission:
    url = f"{_base_url()}/services/{service_id}/permissions"
    resp = requests.post(
        url,
        json={"name": permission_name, "description": description},
        params=_params(realm),
    )
    _check(resp)
    return Permission.model_validate(resp.json())


def create_service_scope(
    service_id: str, scope_name: str, description: str, realm: str
) -> Scope:
    url = f"{_base_url()}/services/{service_id}/scopes"
    resp = requests.post(
        url,
        json={"name": scope_name, "description": description},
        params=_params(realm),
    )
    _check(resp)
    return Scope.model_validate(resp.json())
