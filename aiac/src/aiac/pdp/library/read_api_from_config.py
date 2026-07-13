import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

from aiac.idp.configuration.models import Role, Scope, Service, Subject

load_dotenv(Path(__file__).resolve().parent / ".env")

_CONFIG_ENV_VAR = "AIAC_PDP_CONFIG_PATH"


class Configuration:
    def __init__(self, realm: str) -> None:
        self.realm = realm

    @classmethod
    def for_realm(cls, realm: str) -> "Configuration":
        return cls(realm)

    def _load(self) -> dict:
        env_val = os.getenv(_CONFIG_ENV_VAR)
        if not env_val:
            raise RuntimeError(f"{_CONFIG_ENV_VAR} is not set")
        with open(Path(env_val)) as f:
            return yaml.safe_load(f)

    def get_subjects(self) -> list[Subject]:
        config = self._load()
        subjects_raw = config.get("subjects", config.get("users", []))

        realm_roles_map = {role.name: role for role in self.get_roles()}

        result = []
        for subject in subjects_raw:
            if not isinstance(subject, dict):
                continue
            subject_id = subject.get("id") or subject.get("username")
            username = subject.get("username") or subject_id
            if not subject_id or not username:
                continue

            roles = [
                realm_roles_map[role_name]
                for role_name in subject.get("roles", [])
                if isinstance(role_name, str) and role_name in realm_roles_map
            ]

            result.append(
                Subject(
                    id=subject_id,
                    username=username,
                    email=subject.get("email"),
                    firstName=subject.get("firstName"),
                    lastName=subject.get("lastName"),
                    enabled=subject.get("enabled", True),
                    roles=roles,
                )
            )
        return result

    def get_roles(self) -> list[Role]:
        roles_raw = self._load().get("realm_roles", [])
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

    def get_services(self) -> list[Service]:
        services_raw = self._load().get("services", [])
        result = []
        for service in services_raw:
            if not isinstance(service, dict):
                service_id = str(service)
                result.append(
                    Service(
                        id=service_id,
                        serviceId=service_id,
                        enabled=True,
                    )
                )
                continue

            service_id = service.get("id") or ""

            roles = []
            for role in service.get("roles", []):
                if isinstance(role, dict):
                    role_name = role.get("name", "")
                    role_description = role.get("description") or None
                else:
                    role_name = str(role)
                    role_description = None
                if role_name:
                    roles.append(
                        Role(
                            id=role_name,
                            name=role_name,
                            description=role_description,
                            composite=False,
                        )
                    )

            # Scopes are explicit if provided; otherwise each role maps to a scope of the same name.
            scopes_raw = service.get("scopes")
            if scopes_raw is not None:
                scopes = [
                    Scope(
                        id=s["name"] if isinstance(s, dict) else str(s),
                        name=s["name"] if isinstance(s, dict) else str(s),
                        description=s.get("description") if isinstance(s, dict) else None,
                    )
                    for s in scopes_raw
                ]
            else:
                scopes = [
                    Scope(id=r.name, name=r.name, description=r.description)
                    for r in roles
                ]

            result.append(
                Service(
                    id=service_id,
                    serviceId=service_id,
                    name=service.get("name") or None,
                    description=service.get("description") or None,
                    enabled=service.get("enabled", True),
                    type=service.get("type") or None,
                    roles=roles,
                    scopes=scopes,
                )
            )
        return result

    def get_scopes(self) -> list[Scope]:
        config = self._load()
        scopes_raw = config.get("scopes", [])
        if scopes_raw:
            return [
                Scope(
                    id=s["name"] if isinstance(s, dict) else str(s),
                    name=s["name"] if isinstance(s, dict) else str(s),
                    description=s.get("description") if isinstance(s, dict) else None,
                )
                for s in scopes_raw
            ]

        # Derive from service roles when no top-level scopes section exists.
        seen: set[str] = set()
        result = []
        for service in config.get("services", []):
            if not isinstance(service, dict):
                continue
            for role in service.get("roles", service.get("permissions", [])):
                if isinstance(role, dict):
                    role_name = role.get("name")
                    description = role.get("description")
                else:
                    role_name = str(role)
                    description = None
                if role_name and role_name not in seen:
                    seen.add(role_name)
                    result.append(Scope(id=role_name, name=role_name, description=description))
        return result

    def create_scope(self, scope_name: str, scope_description: str) -> Scope:
        raise NotImplementedError(
            "create_scope is not supported when reading from a static config file. "
            "Use the HTTP-based Configuration class instead."
        )


# Backward compatibility: module-level functions that delegate to Configuration class
def get_subjects(realm: str) -> list[Subject]:
    return Configuration.for_realm(realm).get_subjects()


def get_roles(realm: str) -> list[Role]:
    return Configuration.for_realm(realm).get_roles()


def get_services(realm: str) -> list[Service]:
    return Configuration.for_realm(realm).get_services()


def get_scopes(realm: str) -> list[Scope]:
    return Configuration.for_realm(realm).get_scopes()
