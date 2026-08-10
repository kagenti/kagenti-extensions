from pydantic import BaseModel, ConfigDict

from aiac.idp.configuration.models import Role, Scope, ServiceType


class PolicyRule(BaseModel):
    model_config = ConfigDict(extra="ignore")

    role: Role
    scope: Scope


class ServicePolicyModel(BaseModel):
    """The persistent source of truth — one per service (agent *and* tool), keyed by
    ``service_id``. Holds the service's own identity (owned roles/scopes) plus every inbound
    edge that grants access to its ``owned_scopes``.

    Canonical form: *every rule is an inbound edge on the SPM of the service that owns the
    rule's scope.* An agent's outbound edge is the target's inbound edge (``AR→TS`` is stored on
    ``SPM(T)``, not on ``A``). Because ``UR→TS`` lands durably on ``SPM(T)`` at tool-onboarding —
    no agent required — it can never be lost, which fixes the order-dependence bug that motivated
    the two-layer model.

    ``owned_roles`` / ``owned_scopes`` are the service's own identity, filtered to the
    ``aiac.managed`` marker; they are seeded from the catalog by the PCE (this module only
    defines the shape)."""

    model_config = ConfigDict(extra="ignore")

    service_id: str
    service_type: ServiceType  # Agent | Tool — only Agents get a derived APM
    owned_roles: list[Role]  # this service's own client roles (aiac.managed only)
    owned_scopes: list[Scope]  # this service's exposed scopes (aiac.managed only)
    inbound_rules: list[PolicyRule]  # canonical: every edge granting access to owned_scopes


class AgentPolicyModel(BaseModel):
    """Complete policy definition for a single agent (service).

    **Derived, not persisted.** ``AgentPolicyModel`` is a pure derived projection built by the
    PCE from the relevant ``ServicePolicyModel``s — it is **no longer a persisted entity** (the
    durable source of truth is ``ServicePolicyModel``). Its shape is unchanged so existing
    consumers (PDP Policy Library, Policy Store readers) keep working."""

    model_config = ConfigDict(extra="ignore")

    agent_id: str
    agent_roles: list[Role]
    agent_scopes: list[Scope]
    # Relationship maps are keyed by the referenced entity's string id, so they
    # serialize to JSON natively (no custom key handling needed).
    source_roles: dict[str, list[Role]]  # source service id -> roles granted
    subject_roles: dict[str, list[Role]]  # subject id -> roles held on behalf of
    target_scopes: dict[str, list[Scope]]  # target service id -> scopes permitted
    inbound_rules: list[PolicyRule]
    outbound_rules: list[PolicyRule]
    # (user role, tool scope) pairs — the outbound subject gate; a user holding
    # ``role`` may reach a tool exposing ``scope``. Outbound counterpart of
    # ``inbound_rules`` (which pairs a user role with an *agent* scope).
    outbound_subject_rules: list[PolicyRule] = []


class PolicyModel(BaseModel):
    model_config = ConfigDict(extra="ignore")

    agents: list[AgentPolicyModel]
