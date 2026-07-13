"""Service Policy Builder sub-agent (UC1).

Second of the two stages sequenced by the Service Onboarding Orchestrator, run after
Service Provision. Deterministic (non-LLM): reads the full IdP role/scope universe
*excluding the just-provisioned service's own entities*, flattens roles to their closure
via ``flatten_role`` (3.2) before any PRB call, invokes the Policy Rules Builder for each
applicable pair, and returns a single ``list[PolicyRule]``. It applies nothing — the
Orchestrator/Controller make the single ``compute_and_apply`` (PCE) call afterwards.

IdP access is via the **idp-library** ``Configuration`` (the ``_config`` seam), never the
IdP Configuration Service directly. Own roles/scopes are fetched from the IdP by
``service_id`` (``get_service`` → id-bearing ``Role``/``Scope``), so they are usable as PRB
inputs and can be flattened; ``RoleDefinition``/``ScopeDefinition`` (no id) are never passed
to the PRB.
"""

import os

from fastapi import HTTPException

from aiac.agent.policy_rules_builder.graph import build_role_rules, build_scope_rules
from aiac.agent.shared.roles import flatten_role
from aiac.idp.configuration.api import Configuration
from aiac.idp.configuration.models import ServiceType
from aiac.policy.model.models import PolicyRule


def _config() -> Configuration:
    return Configuration.for_realm(os.getenv("KEYCLOAK_REALM", ""))


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
            service = config.get_service(service_id)
            all_roles = config.get_roles()
            all_scopes = config.get_scopes()
        except Exception as e:
            raise HTTPException(
                502, f"IdP Configuration Service unavailable for service {service_id!r}: {e}"
            )

        own_roles = service.roles
        own_scopes = service.scopes

        own_role_names = {r.name for r in own_roles}
        own_scope_names = {s.name for s in own_scopes}

        other_roles = [r for r in all_roles if r.name not in own_role_names]
        other_scopes = [s for s in all_scopes if s.name not in own_scope_names]

        flattened_other_roles = _flatten_dedup(other_roles)

        rules: list[PolicyRule] = []
        for scope in own_scopes:
            rules.extend(build_scope_rules(flattened_other_roles, scope))
        if service_type is ServiceType.AGENT:
            for own_role in own_roles:
                for role in flatten_role(own_role):
                    rules.extend(build_role_rules(role, other_scopes))
        return rules
