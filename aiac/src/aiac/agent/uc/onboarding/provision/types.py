"""Types for the Service Provision sub-agent (UC1).

``ServiceType`` is intentionally *not* redefined here — it is reused from
``aiac.idp.configuration.models`` (the same enum backing ``Service.type``), so the
sub-agent, the IdP library, and the IdP service share one vocabulary.

``RoleDefinition`` / ``ScopeDefinition`` are deliberately distinct from the IdP
``Role`` / ``Scope`` models: a derived role/scope is a pre-persistence *name + description*
with no Keycloak ``id`` yet, so it cannot be an idp model until ``provision_service`` writes it.
"""

from pydantic import BaseModel

from aiac.idp.configuration.models import ServiceType

__all__ = ["ServiceType", "RoleDefinition", "ScopeDefinition", "ServiceProvision"]


class RoleDefinition(BaseModel):
    name: str
    description: str


class ScopeDefinition(BaseModel):
    name: str
    description: str


class ServiceProvision(BaseModel):
    roles: list[RoleDefinition]
    scopes: list[ScopeDefinition]
    reasoning: str  # machine-generated provenance string
