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
by ``id`` (the Keycloak internal client UUID the ``/apply/service/{id}`` route and
``Trigger.entity_id`` carry — **not** ``serviceId``/clientId, which may be a slash-bearing
SPIFFE URI), so its own roles/scopes are id-bearing ``Role``/``Scope`` usable as PRB inputs
and flattenable.

Candidates are excluded/included by **ownership** (role id / ``scope.serviceId``), never by
name: the focus service's own ``aiac.managed`` roles/scopes are never candidates; other
services' ``aiac.managed`` roles carry ``kind=Agent``; realm roles held by at least one user
(composite-expanded, and not owned by any service) carry ``kind=User``. This keeps
``subject_roles``/``source_roles`` routing correct downstream in the PCE.

The focal-entity resolution itself (the own-scope / candidate-role / other-scope split, and
the IdP-unreachable / unknown-service ``HTTPException(502/404)`` boundary) lives in the shared
``resolve_focal_entities`` (D13) so the read-only Policy Conflict Check diagnostic can reuse
the exact same entity set. This module keeps only the fan-out loop over that set. The local
``_config`` seam is preserved and threaded into the resolver so existing tests patch it as
before.
"""

from aiac.agent.policy_rules_builder.graph import build_role_rules, build_scope_rules
from aiac.agent.shared.focal_entities import resolve_focal_entities
from aiac.agent.shared.roles import flatten_role
from aiac.idp.configuration.api import Configuration
from aiac.idp.configuration.models import ServiceType
from aiac.policy.model.models import PolicyRule


def _config() -> Configuration:
    return Configuration.for_default_realm()


class ServicePolicyBuilder:
    @staticmethod
    def build(service_id: str, service_type: ServiceType) -> list[PolicyRule]:
        # service_type is routed through the parameter (the requested classification for the
        # fan-out), never conflated with focus.type — see #154 AC#6.
        focal = resolve_focal_entities(service_id, service_type, config=_config())

        rules: list[PolicyRule] = []
        for scope in focal.own_scopes:
            rules.extend(build_scope_rules(focal.candidate_roles, scope))
        if service_type is ServiceType.AGENT:
            for own_role in focal.own_roles:
                for role in flatten_role(own_role):
                    rules.extend(build_role_rules(role, focal.other_scopes))
        return rules
