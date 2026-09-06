"""LangGraph state for the Service Provision sub-agent (UC1).

``Trigger`` is the minimal trigger context carried into the graph: the entity id
(Keycloak ``client_id``) that originated the ``aiac.apply.service.{id}`` event / HTTP call.
"""

from pydantic import BaseModel

from aiac.idp.configuration.models import Role, Scope, ServiceType

from .types import ServiceProvision


class Trigger(BaseModel):
    entity_id: str


class OnboardingProvisionState(BaseModel):
    trigger: Trigger

    service_id: str | None = None          # Keycloak internal client UUID (Service.id) = trigger.entity_id (not clientId)
    namespace: str | None = None           # from client.name split in classify_service
    workload_name: str | None = None       # from client.name split in classify_service
    service_type: ServiceType | None = None
    service_provision: ServiceProvision | None = None

    # Created-manifest: exactly the roles/scopes provision_service CREATED on this run (not the
    # ones it reused by name). Consumed by the UC1 compensating rollback so teardown removes only
    # what this run added. Empty on the non-UC1 / no-write paths.
    created_roles: list[Role] = []
    created_scopes: list[Scope] = []
