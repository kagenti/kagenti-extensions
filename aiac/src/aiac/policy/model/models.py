from pydantic import BaseModel, ConfigDict, field_serializer, field_validator

from aiac.idp.configuration.models import Role, Scope, Service, Subject


class PolicyRule(BaseModel):
    model_config = ConfigDict(extra="ignore")

    role: Role
    scope: Scope


class AgentPolicyModel(BaseModel):
    model_config = ConfigDict(extra="ignore")

    agent_id: str
    agent_roles: list[Role]
    agent_scopes: list[Scope]
    subject_roles: dict[Subject, list[Role]]
    source_roles: dict[Service, list[Role]]
    scope_targets: dict[Scope, list[Service]]
    inbound_rules: list[PolicyRule]
    outbound_rules: list[PolicyRule]

    # Pydantic cannot serialize BaseModel instances as dict keys (they serialize
    # to dicts, which are unhashable). Represent these three fields as a list of
    # {key, value} pairs in the serialized form and reconstruct on validation.

    @field_validator("subject_roles", mode="before")
    @classmethod
    def _coerce_subject_roles(cls, v: object) -> dict[Subject, list[Role]]:
        if isinstance(v, list):
            return {
                Subject.model_validate(item["key"]): [Role.model_validate(r) for r in item["value"]]
                for item in v
            }
        return v  # type: ignore[return-value]

    @field_validator("source_roles", mode="before")
    @classmethod
    def _coerce_source_roles(cls, v: object) -> dict[Service, list[Role]]:
        if isinstance(v, list):
            return {
                Service.model_validate(item["key"]): [Role.model_validate(r) for r in item["value"]]
                for item in v
            }
        return v  # type: ignore[return-value]

    @field_validator("scope_targets", mode="before")
    @classmethod
    def _coerce_scope_targets(cls, v: object) -> dict[Scope, list[Service]]:
        if isinstance(v, list):
            return {
                Scope.model_validate(item["key"]): [Service.model_validate(s) for s in item["value"]]
                for item in v
            }
        return v  # type: ignore[return-value]

    @field_serializer("subject_roles")
    def _serialize_subject_roles(self, v: dict[Subject, list[Role]]) -> list[dict]:
        return [{"key": k.model_dump(), "value": [r.model_dump() for r in rs]} for k, rs in v.items()]

    @field_serializer("source_roles")
    def _serialize_source_roles(self, v: dict[Service, list[Role]]) -> list[dict]:
        return [{"key": k.model_dump(), "value": [r.model_dump() for r in rs]} for k, rs in v.items()]

    @field_serializer("scope_targets")
    def _serialize_scope_targets(self, v: dict[Scope, list[Service]]) -> list[dict]:
        return [{"key": k.model_dump(), "value": [s.model_dump() for s in ss]} for k, ss in v.items()]


class PolicyModel(BaseModel):
    model_config = ConfigDict(extra="ignore")

    agents: list[AgentPolicyModel]
