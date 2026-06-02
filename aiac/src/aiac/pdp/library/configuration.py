import os
from pathlib import Path

import requests
from dotenv import load_dotenv

from .models import Subject, Role, Assignments, Service, Scope, Permission

load_dotenv(Path(__file__).resolve().parent / ".env")


def _base_url() -> str:
    return os.getenv("AIAC_PDP_CONFIG_URL", "http://127.0.0.1:7070")


def _params(realm: str) -> dict[str, str]:
    return {"realm": realm}


def _check(resp) -> None:
    if not resp.ok:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text}")


def get_subjects(realm: str) -> list[Subject]:
    resp = requests.get(f"{_base_url()}/subjects", params=_params(realm))
    _check(resp)
    return [Subject.model_validate(s) for s in resp.json()]


def get_roles(realm: str) -> list[Role]:
    resp = requests.get(f"{_base_url()}/roles", params=_params(realm))
    _check(resp)
    return [Role.model_validate(r) for r in resp.json()]


def get_services(realm: str) -> list[Service]:
    resp = requests.get(f"{_base_url()}/services", params=_params(realm))
    _check(resp)
    return [Service.model_validate(s) for s in resp.json()]


def get_scopes(realm: str) -> list[Scope]:
    resp = requests.get(f"{_base_url()}/scopes", params=_params(realm))
    _check(resp)
    return [Scope.model_validate(s) for s in resp.json()]


def get_subject_assignments(subject_id: str, realm: str) -> Assignments:
    resp = requests.get(f"{_base_url()}/subjects/{subject_id}/assignments", params=_params(realm))
    _check(resp)
    return Assignments.model_validate(resp.json())


def get_service_permissions(service_id: str, realm: str) -> list[Permission]:
    resp = requests.get(f"{_base_url()}/services/{service_id}/permissions", params=_params(realm))
    _check(resp)
    return [Permission.model_validate(p) for p in resp.json()]


def get_role_composites(role_name: str, realm: str) -> list[Permission]:
    resp = requests.get(f"{_base_url()}/roles/{role_name}/composites", params=_params(realm))
    _check(resp)
    return [Permission.model_validate(p) for p in resp.json()]
