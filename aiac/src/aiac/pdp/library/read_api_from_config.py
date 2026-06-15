import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

from .models import Subject, Role, Service, Scope

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
        # Try "subjects" first, fall back to "users" for backward compatibility
        subjects_raw = config.get("subjects", [])
        if not subjects_raw:
            subjects_raw = config.get("users", [])

        # Get realm roles for mapping
        realm_roles_map = {}
        for role in self.get_roles():
            realm_roles_map[role.name] = role

        result = []
        for subject in subjects_raw:
            if not isinstance(subject, dict):
                continue
            subject_id = subject.get("id") or subject.get("username")
            username = subject.get("username") or subject_id
            if not subject_id or not username:
                continue
            
            # Parse roles for this subject
            roles_raw = subject.get("roles", [])
            roles = []
            for role_name in roles_raw:
                if isinstance(role_name, str) and role_name in realm_roles_map:
                    roles.append(realm_roles_map[role_name])
            
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
            if isinstance(service, dict):
                service_id = service.get("id") or service.get("service_id") or service.get("serviceId") or ""
                name = service.get("name") or None
                description = service.get("description") or None
                enabled = service.get("enabled", True)
                service_type = service.get("type") or None
                
                # Parse roles for this service
                roles_raw = service.get("roles", [])
                roles = []
                for role in roles_raw:
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
            else:
                service_id = str(service)
                name = None
                description = None
                enabled = True
                service_type = None
                roles = []
            
            result.append(
                Service(
                    id=service_id,
                    name=name,
                    description=description,
                    enabled=enabled,
                    type=service_type,
                    roles=roles,
                )
            )
        return result

    def get_scopes(self) -> list[Scope]:
        config = self._load()
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

    def create_scope(self, service_id: str, scope_name: str, description: str) -> Scope:
        """
        Create a new scope for a service.
        Note: This implementation reads from a static config file and cannot persist changes.
        This method is provided for API compatibility but will raise an error.
        """
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

