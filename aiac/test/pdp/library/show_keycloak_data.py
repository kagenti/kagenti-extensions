"""Print live Keycloak data via aiac-pdp-config-service, exercising all api methods.

Usage:
    python test/pdp/library/show_keycloak_data.py

Requires the service to be reachable at AIAC_PDP_CONFIG_URL (default: http://localhost:7071).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

from aiac.pdp.library.configuration import Configuration
from aiac.pdp.library.models import Role, Scope, Service, Subject

REALM = "kagenti"


def main() -> None:
    cfg = Configuration.for_realm(REALM)

    # --- Subjects (users) ---
    print("=== Subjects ===")
    subjects: list[Subject] = cfg.get_subjects()
    for s in subjects:
        full_name = " ".join(filter(None, [s.firstName, s.lastName])) or "—"
        status = "enabled" if s.enabled else "disabled"
        print(f"  {s.username:<20} {full_name:<25} email={s.email or '—'}  [{status}]")
    print(f"Total: {len(subjects)} subject(s)\n")

    # --- Roles ---
    print("=== Roles ===")
    roles: list[Role] = cfg.get_roles()
    for r in roles:
        composite = "composite" if r.composite else "simple"
        print(f"  {r.name:<30} [{composite}]  desc={r.description or '—'}")
    print(f"Total: {len(roles)} role(s)\n")

    # --- Services (clients) ---
    print("=== Services ===")
    services: list[Service] = cfg.get_services()
    for svc in services:
        status = "enabled" if svc.enabled else "disabled"
        svc_type = svc.type or "—"
        print(f"  {svc.name or svc.id:<40} type={svc_type:<8}  [{status}]  desc={svc.description or '—'}")
    print(f"Total: {len(services)} service(s)\n")

    # --- Scopes ---
    print("=== Scopes ===")
    scopes: list[Scope] = cfg.get_scopes()
    for sc in scopes:
        print(f"  {sc.name:<30} desc={sc.description or '—'}")
    print(f"Total: {len(scopes)} scope(s)\n")


if __name__ == "__main__":
    main()
