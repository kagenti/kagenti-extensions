from enum import Enum

from pydantic import BaseModel, ConfigDict

from aiac.idp.configuration.models import Role, Scope, ServiceType


class RuleEffect(str, Enum):
    """Tags a :class:`PolicyRule` as a grant (``Allow``) or a prohibition (``Deny``).

    A string enum mirroring ``ServiceType`` / ``RoleKind`` style, so ``RuleEffect.ALLOW ==
    "Allow"`` holds and it serializes as the string ``"Allow"`` / ``"Deny"``. A ``Deny`` rule is
    a durable prohibition that subtracts from what the ``Allow`` rules grant (deny-overrides)."""

    ALLOW = "Allow"
    DENY = "Deny"


class PolicyRule(BaseModel):
    model_config = ConfigDict(extra="ignore")

    role: Role
    scope: Scope
    # ``effect`` participates in dedup identity ``(role.id, scope.id, effect)`` so the same
    # ``(role, scope)`` can coexist once as ``Allow`` and once as ``Deny``. Defaulting to
    # ``Allow`` keeps existing allow-only producers working unchanged.
    effect: RuleEffect = RuleEffect.ALLOW


class ServicePolicyModel(BaseModel):
    """The persistent source of truth — one per service (agent *and* tool), keyed by
    ``service_id``. Holds the service's own identity (owned roles/scopes) plus every inbound
    edge (``Allow`` and ``Deny``, in separate parallel lists) touching its ``owned_scopes``.

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
    # Canonical inbound edges, split into two explicitly separated parallel lists (never one
    # intermixed list filtered by ``effect``): every ``Allow`` edge granting access to
    # ``owned_scopes``, and every ``Deny`` edge prohibiting it. A ``Deny`` edge subtracts from
    # what the ``Allow`` edges grant (deny-overrides).
    inbound_allow_rules: list[PolicyRule] = []
    inbound_deny_rules: list[PolicyRule] = []


class AgentPolicyModel(BaseModel):
    """Complete policy definition for a single agent (service).

    **Derived, not persisted.** ``AgentPolicyModel`` is a pure derived projection built by the
    PCE from the relevant ``ServicePolicyModel``s — it is **no longer a persisted entity** (the
    durable source of truth is ``ServicePolicyModel``). Its shape is unchanged so existing
    consumers (PDP Policy Library, Policy Store readers) keep working."""

    model_config = ConfigDict(extra="ignore")

    agent_id: str
    # How the deployed Rego treats a (role, scope) pair that NO rule mentions.
    # DENY (default) reproduces today's least-privilege `default allow := false`.
    # ALLOW opens the default while explicit denies still override (see the OPA
    # generator). Effect-agnostic maps and the 8 rule lists are unchanged.
    # Defaulting to DENY keeps every existing caller and serialized model
    # byte-for-byte compatible (no required-field break).
    default_effect: RuleEffect = RuleEffect.DENY
    # Identity / aggregate maps — effect-agnostic (no allow/deny split). A role or subject that
    # appears **only** in a DENY edge must still be registered here, or the Rego deny lookup
    # cannot resolve it and the prohibition silently fails to fire. Relationship maps are keyed
    # by the referenced entity's string id, so they serialize to JSON natively.
    agent_roles: list[Role]
    agent_scopes: list[Scope]
    source_roles: dict[str, list[Role]]  # source service id -> roles held (effect-agnostic)
    subject_roles: dict[str, list[Role]]  # subject id -> roles held (effect-agnostic)

    # Outbound target maps — split by effect. target service id -> scopes this agent may /
    # must not request on it.
    target_allow_scopes: dict[str, list[Scope]] = {}
    target_deny_scopes: dict[str, list[Scope]] = {}

    # 8 entity×effect rule lists — {inbound subject, inbound source, outbound target, outbound
    # subject} × {allow, deny}. Split explicitly (never one intermixed list filtered by effect);
    # a request is permitted iff some ALLOW gate passes and no DENY gate matches (deny-overrides).
    inbound_subject_allow_rules: list[PolicyRule] = []  # who may call this agent
    inbound_subject_deny_rules: list[PolicyRule] = []  # which subjects are barred
    inbound_source_allow_rules: list[PolicyRule] = []  # which calling services may call
    inbound_source_deny_rules: list[PolicyRule] = []  # which calling services are barred
    outbound_target_allow_rules: list[PolicyRule] = []  # what this agent may call
    outbound_target_deny_rules: list[PolicyRule] = []  # what this agent must not call
    # (user role, tool scope) pairs — the outbound subject gate: which users may / must not reach
    # the agent's targets. Outbound counterpart of the inbound subject rules (user role + agent
    # scope).
    outbound_subject_allow_rules: list[PolicyRule] = []
    outbound_subject_deny_rules: list[PolicyRule] = []


class PolicyModel(BaseModel):
    model_config = ConfigDict(extra="ignore")

    agents: list[AgentPolicyModel]
