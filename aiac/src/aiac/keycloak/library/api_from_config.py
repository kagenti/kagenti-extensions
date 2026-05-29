import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

from .models import User, RealmRole, Client, ClientScope, ClientRole, RoleMappings

load_dotenv(Path(__file__).resolve().parent / ".env")

_CONFIG_ENV_VAR = "AC_CONFIG_PATH"


def _load() -> dict:
    env_val = os.getenv(_CONFIG_ENV_VAR)
    if not env_val:
        raise RuntimeError(f"{_CONFIG_ENV_VAR} is not set")
    with open(Path(env_val)) as f:
        return yaml.safe_load(f)


def _parse_client_roles(roles_raw: list) -> list[ClientRole]:
    result = []
    for r in roles_raw:
        if isinstance(r, dict):
            name = r["name"]
            description = r.get("description") or None
        else:
            name = str(r)
            description = None
        result.append(ClientRole(id=name, name=name, description=description, composite=False, clientRole=True))
    return result


def get_users(realm: str) -> list[User]:
    raise NotImplementedError("get_users is not supported from config")


def get_realm_roles(realm: str) -> list[RealmRole]:
    roles_raw = _load().get("realm_roles", [])
    result = []
    for r in roles_raw:
        if isinstance(r, dict):
            name = r["name"]
            description = r.get("description") or None
        else:
            name = str(r)
            description = None
        result.append(RealmRole(id=name, name=name, description=description, composite=False, clientRole=False))
    return result


def get_clients(realm: str) -> list[Client]:
    clients_raw = _load().get("clients", [])
    result = []
    for c in clients_raw:
        if isinstance(c, dict):
            client_id = c.get("client_id", "")
            name = c.get("name") or None
        else:
            client_id = str(c)
            name = None
        result.append(Client(id=client_id, clientId=client_id, name=name, enabled=True, protocol=None, publicClient=False))
    return result


def get_client_scopes(realm: str) -> list[ClientScope]:
    raise NotImplementedError("get_client_scopes is not supported from config")


def get_user_role_mappings(user_id: str, realm: str) -> RoleMappings:
    raise NotImplementedError("get_user_role_mappings is not supported from config")


def get_client_roles(client_id: str, realm: str) -> list[ClientRole]:
    clients_raw = _load().get("clients", [])
    for c in clients_raw:
        if isinstance(c, dict) and c.get("client_id") == client_id:
            return _parse_client_roles(c.get("roles", []))
    return []


def assign_client_roles(user_id: str, client_id: str, roles: list[ClientRole], realm: str) -> None:
    raise NotImplementedError("assign_client_roles is not supported from config")


def revoke_client_roles(user_id: str, client_id: str, roles: list[ClientRole], realm: str) -> None:
    raise NotImplementedError("revoke_client_roles is not supported from config")


# Convenience helpers (not in api.py) — return dicts for LLM-facing code

def get_client_roles_map(realm: str) -> dict[str, list[dict]]:
    clients_raw = _load().get("clients", [])
    result = {}
    for c in clients_raw:
        if not isinstance(c, dict) or "client_id" not in c:
            continue
        client_id = c["client_id"]
        roles = []
        for r in c.get("roles", []):
            if isinstance(r, dict):
                roles.append({"name": r["name"], "description": r.get("description", "")})
            else:
                roles.append({"name": str(r), "description": ""})
        result[client_id] = roles
    return result


def get_client_names(realm: str) -> list[str]:
    return list(get_client_roles_map(realm).keys())
