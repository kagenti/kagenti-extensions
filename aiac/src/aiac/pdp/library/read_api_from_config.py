import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

from .models import Subject, Role, Service, Scope

load_dotenv(Path(__file__).resolve().parent / ".env")

_CONFIG_ENV_VAR = "AIAC_PDP_CONFIG_PATH"


def _load() -> dict:
    env_val = os.getenv(_CONFIG_ENV_VAR)
    if not env_val:
        raise RuntimeError(f"{_CONFIG_ENV_VAR} is not set")
    with open(Path(env_val)) as f:
        return yaml.safe_load(f)


def get_subjects(realm: str) -> list[Subject]:
    config = _load()
    # Try "subjects" first, fall back to "users" for backward compatibility
    subjects_raw = config.get("subjects", [])
    if not subjects_raw:
        subjects_raw = config.get("users", [])

    result = []
    for subject in subjects_raw:
        if not isinstance(subject, dict):
            continue
        subject_id = subject.get("id") or subject.get("username")
        username = subject.get("username") or subject_id
        if not subject_id or not username:
            continue
        result.append(
            Subject(
                id=subject_id,
                username=username,
                email=subject.get("email"),
                firstName=subject.get("firstName"),
                lastName=subject.get("lastName"),
                enabled=subject.get("enabled", True),
            )
        )
    return result


def get_roles(realm: str) -> list[Role]:
    roles_raw = _load().get("realm_roles", [])
    result = []
    for role in roles_raw:
        if isinstance(role, dict):
            name = role["name"]
            description = role.get("description") or None
        else:
            name = str(role)
            description = None
        result.append(
            Role(
                id=name,
                name=name,
                description=description,
                composite=False,
            )
        )
    return result


def get_services(realm: str) -> list[Service]:
    services_raw = _load().get("services", [])
    result = []
    for service in services_raw:
        if isinstance(service, dict):
            service_id = service.get("id") or service.get("service_id") or service.get("serviceId") or ""
            name = service.get("name") or None
            description = service.get("description") or None
            enabled = service.get("enabled", True)
        else:
            service_id = str(service)
            name = None
            description = None
            enabled = True
        result.append(
            Service(
                id=service_id,
                name=name,
                description=description,
                enabled=enabled,
            )
        )
    return result


def get_scopes(realm: str) -> list[Scope]:
    config = _load()
    scopes_raw = config.get("scopes", [])
    result = []

    # If explicit scopes section exists, use it
    if scopes_raw:
        for scope in scopes_raw:
            if isinstance(scope, dict):
                name = scope["name"]
                description = scope.get("description") or None
            else:
                name = str(scope)
                description = None
            result.append(
                Scope(
                    id=name,
                    name=name,
                    description=description,
                )
            )
    else:
        # If no explicit scopes, derive from service roles (each role gets its own audience scope)
        services_raw = config.get("services", [])
        for service in services_raw:
            if not isinstance(service, dict):
                continue
            roles = service.get("roles", service.get("permissions", []))
            for role in roles:
                if isinstance(role, dict):
                    role_name = role.get("name")
                    if role_name:
                        result.append(
                            Scope(
                                id=role_name,
                                name=role_name,
                                description=role.get("description"),
                            )
                        )

    return result


# NOTE: get_subject_assignments, get_service_permissions, and get_role_composites
# used the deleted Assignments/Permission models. They are stubs pending the
# scope-based permissions redesign (see issues 3.5, 3.7-3.11).

def get_subject_assignments(subject_id: str, realm: str) -> dict:
    return {"realmMappings": [], "serviceMappings": {}}


def get_service_permissions(service_id: str, realm: str) -> list[Role]:
    return []


def get_role_composites(role_name: str, realm: str) -> list[Role]:
    return []
