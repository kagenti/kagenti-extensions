"""Print live Keycloak data via aiac-pdp-config-service, exercising all api methods.

Usage:
    python test/pdp/library/show_keycloak_data.py

Requires the service to be reachable at AIAC_PDP_CONFIG_URL (default: http://localhost:7070).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

from aiac.pdp.library import configuration
from aiac.pdp.library.models import (
    Assignments,
    Permission,
    Role,
    Scope,
    Service,
    Subject,
)

REALM = "kagenti"


def main() -> None:
    # --- Subjects (users) ---
    print("=== Subjects ===")
    subjects: list[Subject] = configuration.get_subjects(REALM)
    for s in subjects:
        full_name = " ".join(filter(None, [s.firstName, s.lastName])) or "—"
        status = "enabled" if s.enabled else "disabled"
        print(f"  {s.username:<20} {full_name:<25} email={s.email or '—'}  [{status}]")
    print(f"Total: {len(subjects)} subject(s)\n")

    # --- Roles ---
    print("=== Roles ===")
    roles: list[Role] = configuration.get_roles(REALM)
    for r in roles:
        composite = "composite" if r.composite else "simple"
        print(f"  {r.name:<30} [{composite}]  desc={r.description or '—'}")
    print(f"Total: {len(roles)} role(s)\n")

    # --- Services (clients) ---
    print("=== Services ===")
    services: list[Service] = configuration.get_services(REALM)
    for svc in services:
        visibility = "public" if svc.publicClient else "confidential"
        status = "enabled" if svc.enabled else "disabled"
        print(f"  {svc.clientId:<40} protocol={svc.protocol or '—'}  [{visibility}]  [{status}]")
    print(f"Total: {len(services)} service(s)\n")

    # --- Scopes ---
    print("=== Scopes ===")
    scopes: list[Scope] = configuration.get_scopes(REALM)
    for sc in scopes:
        print(f"  {sc.name:<30} protocol={sc.protocol or '—'}  desc={sc.description or '—'}")
    print(f"Total: {len(scopes)} scope(s)\n")

    # --- Permissions per service ---
    print("=== Service Permissions ===")
    for svc in services:
        perms: list[Permission] = configuration.get_service_permissions(svc.id, REALM)
        if not perms:
            continue
        print(f"  {svc.clientId}:")
        for p in perms:
            composite = "composite" if p.composite else "simple"
            print(f"    {p.name:<30} [{composite}]  desc={p.description or '—'}")
    print()

    # --- Assignments per subject ---
    print("=== Subject Assignments ===")
    for s in subjects:
        assignments: Assignments = configuration.get_subject_assignments(s.id, REALM)
        realm_role_names: list[str] = [r.name for r in assignments.realmMappings]
        service_map: dict[str, list[str]] = {
            svc_name: [r["name"] for r in mapping.get("mappings", [])]
            for svc_name, mapping in assignments.serviceMappings.items()
            if mapping.get("mappings")
        }
        print(f"  {s.username}:")
        if realm_role_names:
            print(f"    realm : {', '.join(realm_role_names)}")
        for svc_name, perm_names in service_map.items():
            print(f"    {svc_name:<20}: {', '.join(perm_names)}")
        if not realm_role_names and not service_map:
            print("    (no assignments)")


if __name__ == "__main__":
    main()
