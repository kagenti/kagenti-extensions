from __future__ import annotations

from typing import TypedDict

from pydantic import BaseModel

from aiac.pdp.library.models import Role, Scope, Service, Subject


class Permission(BaseModel):
    id: str
    name: str
    description: str | None = None


class Assignments(BaseModel):
    roles: list[Role] = []


class PDPSnapshot(BaseModel):
    subjects: list[Subject] = []
    roles: list[Role] = []
    services: list[Service] = []
    service_permissions: dict[str, list[Permission]] = {}  # service_id → permissions
    service_scopes: list[Scope] = []
    subject_assignments: dict[str, Assignments] = {}  # subject_id → assignments
    role_composites: dict[str, list[Permission]] = {}  # role_name → current composite permissions


class CompositeMapping(BaseModel):
    role_name: str
    service_id: str
    permission_id: str
    permission_name: str


class ProposedDiff(BaseModel):
    add: list[CompositeMapping] = []
    remove: list[CompositeMapping] = []
    reasoning: str = ""


class ValidationVerdict(BaseModel):
    approved: bool
    reason: str | None = None


class TriggerContext(TypedDict):
    trigger_type: str  # e.g. "role/{id}", "service/{id}", "build", "rebuild"
    entity_id: str | None


class BaseAgentState(TypedDict):
    trigger: TriggerContext
    realm: str
    policy_chunks: list[str]
    domain_knowledge_chunks: list[str]
    pdp_snapshot: PDPSnapshot | None
    proposed_diff: ProposedDiff | None
    validation_errors: list[str]
    added: list[CompositeMapping]
    removed: list[CompositeMapping]
    summary: str
