from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator


class ServiceType(str, Enum):
    """Canonical service-type vocabulary, shared by the IdP library, the IdP service, and the
    AIAC agent sub-agents. Values are capitalized (``Agent``/``Tool``) to match the Keycloak
    ``client.type`` attribute; as a ``str`` enum, ``ServiceType.AGENT == "Agent"`` holds, so it
    is a drop-in for the former ``Literal["Agent", "Tool"]``. The operator's ``rossoctl.io/type``
    pod label is lowercase (``agent``/``tool``) and is normalized to a member via
    ``ServiceType(label.capitalize())`` at classification time."""

    AGENT = "Agent"
    TOOL = "Tool"


class RoleKind(str, Enum):
    """Whether a role is held by users or by agent service accounts. Mirrors ``ServiceType``'s
    capitalized ``str``-enum style, so ``RoleKind.USER == "User"`` holds.

    - ``USER`` ⇔ a Keycloak **realm** role; ``Role.actorIds`` are the holder usernames.
    - ``AGENT`` ⇔ a Keycloak **client** role on an agent's client; ``Role.actorIds`` are the
      owning agent ``serviceId``(s) (usually one).

    The client/realm ⇔ agent/user invariant (Assumption 3) and the cross-kind invariant
    (Assumption 1) are enforced upstream at the Keycloak IdP construction boundary (handoff 02),
    not by the local ``Role`` validator."""

    USER = "User"
    AGENT = "Agent"


# AIAC naming convention: every role and client scope AIAC provisions carries the Keycloak
# attribute ``aiac.managed`` with value ``true``. Keycloak's own built-ins (default client
# scopes, ``default-roles-<realm>``) never carry it, so consumers filter on this marker to
# distinguish AIAC-provisioned entities. Realm-role attribute values are lists of strings
# (``{"aiac.managed": ["true"]}``); client-scope attribute values are plain strings
# (``{"aiac.managed": "true"}``) — the helper below tolerates both shapes.
AIAC_MANAGED_ATTRIBUTE = "aiac.managed"

# Keycloak attribute that carries a service's (client's) type. AIAC calls the concept
# "service type" everywhere (``Service.type`` ∈ {``Agent``,``Tool``}); the underlying Keycloak
# client attribute is named ``client.type``. The value is a plain string (``"Agent"``/``"Tool"``,
# capitalized to match ``ServiceType``) — a list value fails resolution → type ``None``.
SERVICE_TYPE_ATTRIBUTE = "client.type"


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
    # SPM routing metadata. ``kind`` distinguishes a user-held realm role from an agent-held
    # client role; ``actorIds`` are the holder usernames (USER) or owning agent serviceId(s)
    # (AGENT). Populated deeply from Keycloak at the IdP construction boundary (handoff 02);
    # defaulted here so Wave 1 stays backward-compatible with existing Role construction sites.
    kind: RoleKind = RoleKind.USER
    actorIds: list[str] = []

    @property
    def aiac_managed(self) -> bool:
        """True when this role carries the ``aiac.managed`` provisioning marker."""
        return _is_aiac_managed(self.attributes)

    @model_validator(mode="after")
    def _validate_kind_and_actor_ids(self) -> "Role":
        """Enforce the invariants visible locally: ``kind`` is a valid ``RoleKind`` and
        ``actorIds`` is a ``list[str]``. The cross-kind invariant (Assumption 1) and the
        client/realm ⇔ agent/user invariant (Assumption 3) require raw Keycloak facts and are
        enforced upstream at construction (handoff 02), not here."""
        if not isinstance(self.kind, RoleKind):
            raise ValueError("Role.kind must be a RoleKind")
        if not isinstance(self.actorIds, list) or not all(isinstance(a, str) for a in self.actorIds):
            raise ValueError("Role.actorIds must be a list[str]")
        return self


class Service(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    serviceId: str
    name: str | None = None
    description: str | None = None
    enabled: bool
    type: ServiceType | None = None
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

        # Resolve service type. Precedence: explicit ``type`` (already set, skipped here) →
        # Keycloak ``client.type`` attribute (plain string ∈ {Agent,Tool}) → None. A
        # list-valued or unrecognized attribute fails the string check → None. clientId
        # shape (e.g. ``spiffe://``) is not consulted: it signals SPIRE-enablement, not
        # agent-vs-tool — the operator's ``rossoctl.io/type`` label (persisted as
        # ``client.type``) is the authoritative type signal.
        if data.get("type") is None:
            attrs = data.get("attributes") or {}
            stored_type = attrs.get(SERVICE_TYPE_ATTRIBUTE)
            if stored_type in ("Agent", "Tool"):
                updates["type"] = stored_type

        return {**data, **updates} if updates else data


class Scope(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    description: str | None = None
    attributes: dict[str, Any] = {}
    # The single owning service's serviceId — the SPM routing key: a rule ``(role, scope)``
    # routes to ``SPM(scope.serviceId)`` (Assumption 2: a scope has exactly one owner).
    # Populated at the IdP construction boundary (handoff 02); defaulted here so Wave 1 stays
    # backward-compatible with existing Scope construction sites.
    serviceId: str = ""

    @property
    def aiac_managed(self) -> bool:
        """True when this scope carries the ``aiac.managed`` provisioning marker."""
        return _is_aiac_managed(self.attributes)


Subject.model_rebuild()
Role.model_rebuild()
Service.model_rebuild()
