from pydantic import BaseModel, ConfigDict

from aiac.idp.configuration.models import Role, Scope


class PolicyRule(BaseModel):
    model_config = ConfigDict(extra="ignore")

    role: Role
    scope: Scope


class AgentPolicyModel(BaseModel):
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
