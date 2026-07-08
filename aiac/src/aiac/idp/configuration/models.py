from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator

# AIAC naming convention: every role and client scope AIAC provisions carries the Keycloak
# attribute ``aiac.managed`` with value ``true``. Keycloak's own built-ins (default client
# scopes, ``default-roles-<realm>``) never carry it, so consumers filter on this marker to
# distinguish AIAC-provisioned entities. Realm-role attribute values are lists of strings
# (``{"aiac.managed": ["true"]}``); client-scope attribute values are plain strings
# (``{"aiac.managed": "true"}``) — the helper below tolerates both shapes.
AIAC_MANAGED_ATTRIBUTE = "aiac.managed"


def _is_aiac_managed(attributes: dict[str, Any]) -> bool:
    value = attributes.get(AIAC_MANAGED_ATTRIBUTE)
    if isinstance(value, list):
        return "true" in value
    return value == "true"


class Subject(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    username: str
    email: str | None = None
    firstName: str | None = None
    lastName: str | None = None
    enabled: bool
    roles: list["Role"] = []


class Role(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    description: str | None = None
    composite: bool
    childRoles: list["Role"] = []
    attributes: dict[str, Any] = {}

    @property
    def aiac_managed(self) -> bool:
        """True when this role carries the ``aiac.managed`` provisioning marker."""
        return _is_aiac_managed(self.attributes)


class Service(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    serviceId: str
    name: str | None = None
    description: str | None = None
    enabled: bool
    type: Literal["Agent", "Tool"] | None = None
    roles: list["Role"] = []
    scopes: list["Scope"] = []

    @model_validator(mode="before")
    @classmethod
    def _resolve_keycloak_fields(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        updates: dict[str, Any] = {}

        # Keycloak uses clientId as the identifier; name is a display name
        # that is often a localisation placeholder like ${client_account}.
        client_id = data.get("clientId")
        name = data.get("name")
        if client_id and (not name or str(name).startswith("${")):
            updates["name"] = client_id

        # Surface Keycloak's human-readable clientId as serviceId (id stays the UUID).
        if client_id and not data.get("serviceId"):
            updates["serviceId"] = client_id

        # Resolve service type: explicit Keycloak attribute takes precedence,
        # then SPIFFE-format clientId implies an agent workload.
        if data.get("type") is None:
            attrs = data.get("attributes") or {}
            stored_type = attrs.get("kagenti.service.type")
            if stored_type in ("Agent", "Tool"):
                updates["type"] = stored_type
            elif client_id and str(client_id).startswith("spiffe://"):
                updates["type"] = "Agent"

        return {**data, **updates} if updates else data


class Scope(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    description: str | None = None
    attributes: dict[str, Any] = {}

    @property
    def aiac_managed(self) -> bool:
        """True when this scope carries the ``aiac.managed`` provisioning marker."""
        return _is_aiac_managed(self.attributes)


Subject.model_rebuild()
Role.model_rebuild()
Service.model_rebuild()
