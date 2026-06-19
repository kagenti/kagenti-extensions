from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator


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
    mappedScopes: list["Scope"] = []


class Service(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    serviceId: str | None = None
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


Subject.model_rebuild()
Role.model_rebuild()
Service.model_rebuild()
