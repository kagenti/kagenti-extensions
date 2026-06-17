"""Print live Keycloak data via aiac-pdp-config-service, exercising all api methods.

Usage:
    python test/pdp/library/show_keycloak_data.py

Requires the service to be reachable at AIAC_PDP_CONFIG_URL (default: http://localhost:7071).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

from aiac.pdp.library.configuration.api import Configuration
from aiac.pdp.library.configuration.models import Role, Scope, Service, Subject

REALM = "kagenti"


def _fmt_roles(roles: list[Role], indent: int = 4) -> str:
    if not roles:
        return " " * indent + "—"
    pad = " " * indent
    lines = []
    for r in roles:
        composite = "composite" if r.composite else "simple"
        lines.append(f"{pad}{r.name} (id={r.id})  [{composite}]  desc={r.description or '—'}")
        if r.childRoles:
            for cr in r.childRoles:
                lines.append(f"{pad}  child: {cr.name} (id={cr.id})")
        if r.mappedScopes:
            for ms in r.mappedScopes:
                lines.append(f"{pad}  scope: {ms.name} (id={ms.id})")
    return "\n".join(lines)


def _fmt_scopes(scopes: list[Scope], indent: int = 4) -> str:
    if not scopes:
        return " " * indent + "—"
    pad = " " * indent
    return "\n".join(f"{pad}{sc.name} (id={sc.id})  desc={sc.description or '—'}" for sc in scopes)


def main() -> None:
    cfg = Configuration.for_realm(REALM)

    # --- Subjects (users) ---
    print("=== Subjects ===")
    subjects: list[Subject] = cfg.get_subjects()
    for s in subjects:
        full_name = " ".join(filter(None, [s.firstName, s.lastName])) or "—"
        status = "enabled" if s.enabled else "disabled"
        print(f"  id={s.id}")
        print(f"    username  : {s.username}")
        print(f"    name      : {full_name}")
        print(f"    email     : {s.email or '—'}")
        print(f"    status    : {status}")
        if s.roles:
            print("    roles:")
            print(_fmt_roles(s.roles, indent=6))
        else:
            print("    roles     : —")
    print(f"Total: {len(subjects)} subject(s)\n")

    # --- Roles ---
    print("=== Roles ===")
    roles: list[Role] = cfg.get_roles()
    for r in roles:
        composite = "composite" if r.composite else "simple"
        print(f"  id={r.id}")
        print(f"    name        : {r.name}")
        print(f"    description : {r.description or '—'}")
        print(f"    type        : {composite}")
        if r.childRoles:
            print("    childRoles:")
            for cr in r.childRoles:
                print(f"      {cr.name} (id={cr.id})")
        else:
            print("    childRoles  : —")
        if r.mappedScopes:
            print("    mappedScopes:")
            for ms in r.mappedScopes:
                print(f"      {ms.name} (id={ms.id})  desc={ms.description or '—'}")
        else:
            print("    mappedScopes: —")
    print(f"Total: {len(roles)} role(s)\n")

    # --- Services (clients) ---
    print("=== Services ===")
    services: list[Service] = cfg.get_services()
    for svc in services:
        status = "enabled" if svc.enabled else "disabled"
        print(f"  id={svc.id}")
        print(f"    name        : {svc.name or '—'}")
        print(f"    description : {svc.description or '—'}")
        print(f"    status      : {status}")
        print(f"    type        : {svc.type or '—'}")
        if svc.roles:
            print("    roles:")
            print(_fmt_roles(svc.roles, indent=6))
        else:
            print("    roles       : —")
        if svc.scopes:
            print("    scopes:")
            print(_fmt_scopes(svc.scopes, indent=6))
        else:
            print("    scopes      : —")
    print(f"Total: {len(services)} service(s)\n")

    # --- Scopes ---
    print("=== Scopes ===")
    scopes: list[Scope] = cfg.get_scopes()
    for sc in scopes:
        print(f"  id={sc.id}")
        print(f"    name        : {sc.name}")
        print(f"    description : {sc.description or '—'}")
    print(f"Total: {len(scopes)} scope(s)\n")


if __name__ == "__main__":
    main()
