import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

from .models import Subject, Role, Assignments, Service, Scope, Permission

load_dotenv(Path(__file__).resolve().parent / ".env")

_CONFIG_ENV_VAR = "AIAC_PDP_CONFIG_PATH"


def _load() -> dict:
    env_val = os.getenv(_CONFIG_ENV_VAR)
    if not env_val:
        raise RuntimeError(f"{_CONFIG_ENV_VAR} is not set")
    with open(Path(env_val)) as f:
        return yaml.safe_load(f)


def get_subjects(realm: str) -> list[Subject]:
    subjects_raw = _load().get("subjects", [])
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
                clientRole=False,
            )
        )
    return result


def get_services(realm: str) -> list[Service]:
    services_raw = _load().get("services", [])
    result = []
    for service in services_raw:
        if isinstance(service, dict):
            service_id = service.get("id") or service.get("service_id") or service.get("serviceId") or ""
            client_id = service.get("service_id") or service.get("serviceId") or service_id
            name = service.get("name") or None
            description = service.get("description") or None
            enabled = service.get("enabled", True)
            protocol = service.get("protocol")
            public_client = service.get("publicClient", False)
        else:
            service_id = str(service)
            client_id = service_id
            name = None
            description = None
            enabled = True
            protocol = None
            public_client = False
        result.append(
            Service(
                id=service_id,
                clientId=client_id,
                name=name,
                description=description,
                enabled=enabled,
                protocol=protocol,
                publicClient=public_client,
            )
        )
    return result


def get_scopes(realm: str) -> list[Scope]:
    scopes_raw = _load().get("scopes", [])
    result = []
    for scope in scopes_raw:
        if isinstance(scope, dict):
            name = scope["name"]
            description = scope.get("description") or None
            protocol = scope.get("protocol")
        else:
            name = str(scope)
            description = None
            protocol = None
        result.append(
            Scope(
                id=name,
                name=name,
                description=description,
                protocol=protocol,
            )
        )
    return result


def get_subject_assignments(subject_id: str, realm: str) -> Assignments:
    assignments_raw = _load().get("subject_assignments", {})
    subject_assignments = assignments_raw.get(subject_id, {})
    realm_mappings_raw = subject_assignments.get("realmMappings", [])
    realm_mappings = []
    for role in realm_mappings_raw:
        if isinstance(role, dict):
            name = role["name"]
            description = role.get("description") or None
        else:
            name = str(role)
            description = None
        realm_mappings.append(
            Role(
                id=name,
                name=name,
                description=description,
                composite=False,
                clientRole=False,
            )
        )
    return Assignments(
        realmMappings=realm_mappings,
        serviceMappings=subject_assignments.get("serviceMappings", {}),
    )


def get_service_permissions(service_id: str, realm: str) -> list[Permission]:
    services_raw = _load().get("services", [])
    for service in services_raw:
        if not isinstance(service, dict):
            continue
        current_service_id = service.get("id") or service.get("service_id") or service.get("serviceId")
        if current_service_id != service_id:
            continue
        permissions = []
        for permission in service.get("permissions", service.get("roles", [])):
            if isinstance(permission, dict):
                name = permission["name"]
                description = permission.get("description") or None
            else:
                name = str(permission)
                description = None
            permissions.append(
                Permission(
                    id=name,
                    name=name,
                    description=description,
                    composite=False,
                    clientRole=True,
                )
            )
        return permissions
    return []


def get_role_composites(role_name: str, realm: str) -> list[Permission]:
    roles_raw = _load().get("roles", [])
    for role in roles_raw:
        if not isinstance(role, dict) or role.get("name") != role_name:
            continue
        composites = []
        for composite in role.get("composites", []):
            if isinstance(composite, dict):
                name = composite["name"]
                description = composite.get("description") or None
            else:
                name = str(composite)
                description = None
            composites.append(
                Permission(
                    id=name,
                    name=name,
                    description=description,
                    composite=False,
                    clientRole=True,
                )
            )
        return composites
    return []

# Made with Bob
