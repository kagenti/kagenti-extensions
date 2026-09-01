"""Focal-entity resolution for a target service (shared).

Extracted from ``ServicePolicyBuilder.build()`` (D13) so both the live Service Policy
Builder and the read-only Policy Conflict Check diagnostic resolve the **same** typed entity
set from the live IdP catalog. This is a **pure extraction** — the resolution logic is
byte-for-byte the same split the builder performed inline; no live behavior changes.

The focus service is resolved from ``get_services()`` by ``id`` (the Keycloak internal client
UUID the ``/apply/service/{id}`` route and ``Trigger.entity_id`` carry — **not**
``serviceId``/clientId, which may be a slash-bearing SPIFFE URI). Candidates are
excluded/included by **ownership** (role id / ``scope.serviceId``), never by name:

- ``own_roles`` / ``own_scopes`` — the focus service's own ``aiac.managed`` roles/scopes.
- ``candidate_roles`` — the flattened, de-duplicated union of (a) other services'
  ``aiac.managed`` roles (``kind=Agent``) and (b) realm roles held by at least one user
  (composite-expanded, and not owned by any service; ``kind=User``).
- ``other_scopes`` — other services' ``aiac.managed`` scopes, sourced from ``get_services()``
  so each scope carries its owning ``serviceId`` (the SPM routing key the PCE needs).

IdP access is via the **idp-library** ``Configuration`` seam. Callers that already hold a
``Configuration`` (e.g. the live builder, whose ``_config`` seam existing tests patch) pass it
in via ``config``; callers that don't (e.g. the diagnostic) let the resolver create the
default-realm one. ``HTTPException(502)`` is raised on IdP-unreachable, ``HTTPException(404)``
on unknown service — this is the feature's pre-survey HTTP boundary.
"""

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict

from aiac.agent.shared.roles import flatten_role
from aiac.idp.configuration.api import Configuration
from aiac.idp.configuration.models import Role, Scope, ServiceType


class FocalEntitySet(BaseModel):
    """Typed result of :func:`resolve_focal_entities` — the focus service's own entities plus
    the candidate universes it maps against. ``service_type`` is echoed from the caller's
    parameter (the requested classification for the fan-out), **not** ``focus.type``."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    own_scopes: list[Scope]
    own_roles: list[Role]
    candidate_roles: list[Role]
    other_scopes: list[Scope]
    service_type: ServiceType


def _config() -> Configuration:
    return Configuration.for_default_realm()


def _flatten_dedup(roles: list[Role]) -> list[Role]:
    """Union of every role's closure, de-duplicated by ``role.id``."""
    out: list[Role] = []
    seen: set[str] = set()
    for role in roles:
        for member in flatten_role(role):
            if member.id not in seen:
                seen.add(member.id)
                out.append(member)
    return out


def resolve_focal_entities(
    service_id: str,
    service_type: ServiceType,
    *,
    config: Configuration | None = None,
) -> FocalEntitySet:
    """Resolve the focus service's own entities + candidate universes from the live IdP catalog.

    ``service_id`` is the Keycloak internal client UUID (``Service.id``), not the
    human-readable ``serviceId``/clientId. ``service_type`` is the requested classification and
    is echoed onto the result unchanged (the caller routes on it — it is **not** derived from
    ``focus.type``). Pass ``config`` to reuse an existing ``Configuration`` seam; otherwise the
    default-realm one is created.

    Raises ``HTTPException(502)`` when the IdP Configuration Service is unreachable and
    ``HTTPException(404)`` when ``service_id`` is absent from the catalog.
    """
    config = config or _config()

    try:
        services = config.get_services()
        subjects = config.get_subjects()
    except Exception as e:
        raise HTTPException(
            502, f"IdP Configuration Service unavailable for service {service_id!r}: {e}"
        )

    # The trigger id is the Keycloak internal client UUID (Service.id), not the human-readable
    # clientId (Service.serviceId): the /apply/service/{id} route is keyed on the UUID because a
    # clientId can be a slash-bearing SPIFFE URI the single-segment route cannot carry.
    focus = next((s for s in services if s.id == service_id), None)
    if focus is None:
        raise HTTPException(404, f"service {service_id!r} not found in IdP catalog")

    own_roles = [r for r in focus.roles if r.aiac_managed]
    own_scopes = [s for s in focus.scopes if s.aiac_managed]

    # kind=Agent rides through unchanged from get_services() → routes to source_roles in the PCE.
    other_agent_roles = [
        r
        for other in services
        if other.serviceId != focus.serviceId
        for r in other.roles
        if r.aiac_managed
    ]

    # User roles are membership-derived, not aiac.managed: a realm role qualifies iff a user
    # holds it directly or via a composite parent they hold, and no service owns it.
    service_owned_ids = {r.id for s in services for r in s.roles}
    user_roles_by_id: dict[str, Role] = {}
    for subject in subjects:
        for role in subject.roles:
            # NB: flatten_role on an agent composite role would yield children whose kind
            # defaults to User (composites endpoint doesn't carry per-service kind) — a latent
            # edge case if a user is ever assigned a composite agent role. Not hit here.
            for member in flatten_role(role):
                if member.id not in service_owned_ids:
                    user_roles_by_id[member.id] = member
    user_roles = list(user_roles_by_id.values())

    # Other services' aiac.managed scopes, sourced from get_services() (mirroring
    # other_agent_roles) so each scope carries its owning serviceId — the SPM routing key the
    # PCE needs. The global get_scopes() endpoint returns scopes with an empty serviceId, which
    # would both (a) fail to exclude the focus's own scopes (``"" != focus.serviceId`` is always
    # true) and (b) route any resulting rule to ``SPM("")``, a 422 dead-end.
    other_scopes = [
        s
        for other in services
        if other.serviceId != focus.serviceId
        for s in other.scopes
        if s.aiac_managed
    ]

    candidate_roles = _flatten_dedup(user_roles + other_agent_roles)

    return FocalEntitySet(
        own_scopes=own_scopes,
        own_roles=own_roles,
        candidate_roles=candidate_roles,
        other_scopes=other_scopes,
        service_type=service_type,
    )
