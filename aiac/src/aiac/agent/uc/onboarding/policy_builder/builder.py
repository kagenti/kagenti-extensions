"""Service Policy Builder sub-agent (UC1).

Second of the two stages sequenced by the Service Onboarding Orchestrator, run after
Service Provision. Deterministic (non-LLM): sources candidates from the same worldview as
the Policy Computation Engine — ``get_services()`` for correct ``kind``/ownership, plus
``get_subjects()`` for membership-derived user roles — flattens roles to their closure via
``flatten_role`` (3.2) before any PRB call, invokes the Policy Rules Builder for each
applicable pair, and returns a single ``list[PolicyRule]``. It applies nothing — the
Orchestrator/Controller make the single ``compute_and_apply`` (PCE) call afterwards.

IdP access is via the **idp-library** ``Configuration`` (the ``_config`` seam), never the
IdP Configuration Service directly. The focus service is resolved from ``get_services()``
by ``serviceId`` (the same identity the PCE catalogs on), so its own roles/scopes are
id-bearing ``Role``/``Scope`` usable as PRB inputs and flattenable.

Candidates are excluded/included by **ownership** (role id / ``scope.serviceId``), never by
name: the focus service's own ``aiac.managed`` roles/scopes are never candidates; other
services' ``aiac.managed`` roles carry ``kind=Agent``; realm roles held by at least one user
(composite-expanded, and not owned by any service) carry ``kind=User``. This keeps
``subject_roles``/``source_roles`` routing correct downstream in the PCE.
"""

from fastapi import HTTPException

from aiac.agent.policy_rules_builder.graph import build_role_rules, build_scope_rules
from aiac.agent.shared.roles import flatten_role
from aiac.idp.configuration.api import Configuration
from aiac.idp.configuration.models import Role, ServiceType
from aiac.policy.model.models import PolicyRule


def _config() -> Configuration:
    return Configuration.for_default_realm()


def _flatten_dedup(roles):
    """Union of every role's closure, de-duplicated by ``role.id``."""
    out = []
    seen: set[str] = set()
    for role in roles:
        for member in flatten_role(role):
            if member.id not in seen:
                seen.add(member.id)
                out.append(member)
    return out


class ServicePolicyBuilder:
    @staticmethod
    def build(service_id: str, service_type: ServiceType) -> list[PolicyRule]:
        config = _config()

        try:
            services = config.get_services()
            all_scopes = config.get_scopes()
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

        other_scopes = [s for s in all_scopes if s.aiac_managed and s.serviceId != focus.serviceId]

        candidate_roles = _flatten_dedup(user_roles + other_agent_roles)

        rules: list[PolicyRule] = []
        for scope in own_scopes:
            rules.extend(build_scope_rules(candidate_roles, scope))
        if service_type is ServiceType.AGENT:
            for own_role in own_roles:
                for role in flatten_role(own_role):
                    rules.extend(build_role_rules(role, other_scopes))
        return rules
