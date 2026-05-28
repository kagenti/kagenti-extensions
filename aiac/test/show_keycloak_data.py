"""Print live Keycloak data via aiac-keycloak-service, exercising all api methods.

Usage:
    python test/show_keycloak_data.py

Requires the service to be reachable at AC_SERVICE_URL (default: http://localhost:7070).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from aiac.keycloak.library import api
from aiac.keycloak.library.models import (
    Client,
    ClientRole,
    ClientScope,
    RealmRole,
    RoleMappings,
    User,
)

REALM = "kagenti"


def main() -> None:
    # --- Users ---
    print("=== Users ===")
    users: list[User] = api.get_users(realm=REALM)
    for u in users:
        full_name = " ".join(filter(None, [u.firstName, u.lastName])) or "—"
        status = "enabled" if u.enabled else "disabled"
        print(f"  {u.username:<20} {full_name:<25} email={u.email or '—'}  [{status}]")
    print(f"Total: {len(users)} user(s)\n")

    # --- Realm roles ---
    print("=== Realm Roles ===")
    realm_roles: list[RealmRole] = api.get_realm_roles(realm=REALM)
    for r in realm_roles:
        composite = "composite" if r.composite else "simple"
        print(f"  {r.name:<30} [{composite}]  desc={r.description or '—'}")
    print(f"Total: {len(realm_roles)} realm role(s)\n")

    # --- Clients ---
    print("=== Clients ===")
    clients: list[Client] = api.get_clients(realm=REALM)
    for c in clients:
        visibility = "public" if c.publicClient else "confidential"
        status = "enabled" if c.enabled else "disabled"
        print(f"  {c.clientId:<40} protocol={c.protocol or '—'}  [{visibility}]  [{status}]")
    print(f"Total: {len(clients)} client(s)\n")

    # --- Client scopes ---
    print("=== Client Scopes ===")
    scopes: list[ClientScope] = api.get_client_scopes(realm=REALM)
    for s in scopes:
        print(f"  {s.name:<30} protocol={s.protocol or '—'}  desc={s.description or '—'}")
    print(f"Total: {len(scopes)} scope(s)\n")

    # --- Client roles per client ---
    print("=== Client Roles ===")
    for c in clients:
        roles: list[ClientRole] = api.get_client_roles(c.id, realm=REALM)
        if not roles:
            continue
        print(f"  {c.clientId}:")
        for r in roles:
            composite = "composite" if r.composite else "simple"
            print(f"    {r.name:<30} [{composite}]  desc={r.description or '—'}")
    print()

    # --- Role mappings per user ---
    print("=== User Role Mappings ===")
    for u in users:
        mappings: RoleMappings = api.get_user_role_mappings(u.id, realm=REALM)
        realm_roles_list: list[str] = [r.name for r in mappings.realmMappings]
        client_map: dict[str, list[str]] = {
            client_name: [r["name"] for r in mapping.get("mappings", [])]
            for client_name, mapping in mappings.clientMappings.items()
            if mapping.get("mappings")
        }
        print(f"  {u.username}:")
        if realm_roles_list:
            print(f"    realm : {', '.join(realm_roles_list)}")
        for client_name, role_names in client_map.items():
            print(f"    {client_name:<20}: {', '.join(role_names)}")
        if not realm_roles_list and not client_map:
            print("    (no roles)")


if __name__ == "__main__":
    main()
