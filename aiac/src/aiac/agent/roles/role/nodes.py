import os

from fastapi import HTTPException
from langchain_openai import ChatOpenAI

from aiac.agent.shared.state import (
    BaseAgentState,
    CompositeMapping,
    PDPSnapshot,
    Permission,
    ProposedDiff,
    ValidationVerdict,
)
from aiac.pdp.library.configuration import Configuration
from aiac.pdp.library.policy import Policy

_MAX_CHANGES_DEFAULT = 50


def fetch_pdp_state(state: BaseAgentState) -> dict:
    realm = state["realm"]
    trigger = state["trigger"]
    entity_id = trigger["entity_id"]

    try:
        cfg = Configuration.for_realm(realm)
        services = cfg.get_services()
        roles = cfg.get_roles()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"PDP Configuration Service unavailable: {exc}") from exc

    service_permissions: dict[str, list[Permission]] = {}
    for svc in services:
        service_permissions[svc.id] = [
            Permission(id=r.id, name=r.name, description=r.description)
            for r in svc.roles
        ]

    role_composites: dict[str, list[Permission]] = {}
    affected_role = next((r for r in roles if r.id == entity_id), None)
    if affected_role is not None:
        role_composites[affected_role.name] = [
            Permission(id=child.id, name=child.name, description=child.description)
            for child in affected_role.childRoles
        ]

    snapshot = PDPSnapshot(
        roles=roles,
        services=services,
        service_permissions=service_permissions,
        role_composites=role_composites,
    )
    return {**state, "pdp_snapshot": snapshot}


def propose_mappings(state: BaseAgentState) -> dict:
    snapshot: PDPSnapshot = state["pdp_snapshot"]
    policy_chunks = state["policy_chunks"]
    domain_chunks = state["domain_knowledge_chunks"]
    trigger = state["trigger"]
    entity_id = trigger["entity_id"]

    affected_role = next((r for r in snapshot.roles if r.id == entity_id), None)
    role_name = affected_role.name if affected_role else entity_id

    context = "\n".join([
        "## Access Control Policy",
        *policy_chunks,
        "## Domain Knowledge",
        *domain_chunks,
        "## Current PDP State",
        f"Affected role: {role_name}",
        f"Services: {[s.id for s in snapshot.services]}",
        f"Permissions: {snapshot.service_permissions}",
        f"Current composites for {role_name}: {snapshot.role_composites.get(role_name, [])}",
    ])

    from aiac.agent.roles.role.prompts import PLANNER_SYSTEM

    try:
        llm = ChatOpenAI(model=os.getenv("OPENAI_MODEL_NAME", "gpt-4o-mini"), temperature=0)
        diff: ProposedDiff = llm.with_structured_output(ProposedDiff).invoke(
            [
                {"role": "system", "content": PLANNER_SYSTEM},
                {"role": "user", "content": context},
            ]
        )
    except Exception as exc:
        raise HTTPException(status_code=504, detail=f"LLM unavailable: {exc}") from exc

    return {**state, "proposed_diff": diff}


def validate_mappings(state: BaseAgentState) -> dict:
    snapshot: PDPSnapshot = state["pdp_snapshot"]
    diff: ProposedDiff = state["proposed_diff"]
    trigger = state["trigger"]
    entity_id = trigger["entity_id"]

    affected_role = next((r for r in snapshot.roles if r.id == entity_id), None)
    affected_role_name = affected_role.name if affected_role else entity_id

    errors: list[str] = []
    all_mappings = list(diff.add) + list(diff.remove)

    # 1. Scope check — all mappings must be for the affected role
    out_of_scope = [m for m in all_mappings if m.role_name != affected_role_name]
    if out_of_scope:
        names = {m.role_name for m in out_of_scope}
        errors.append(f"Scope violation: diff touches roles outside affected role '{affected_role_name}': {names}")
        return {**state, "validation_errors": errors}

    # 2. Existence check
    known_role_names = {r.name for r in snapshot.roles}
    for m in all_mappings:
        if m.role_name not in known_role_names:
            errors.append(f"Existence: unknown role '{m.role_name}'")
        elif m.service_id not in snapshot.service_permissions:
            errors.append(f"Existence: unknown service '{m.service_id}'")
        elif not any(p.id == m.permission_id for p in snapshot.service_permissions.get(m.service_id, [])):
            errors.append(f"Existence: unknown permission '{m.permission_id}' in service '{m.service_id}'")

    if errors:
        return {**state, "validation_errors": errors}

    # 3. Safety guard
    max_changes = int(os.getenv("MAX_CHANGES_PER_RUN", str(_MAX_CHANGES_DEFAULT)))
    if len(all_mappings) > max_changes:
        errors.append(f"Safety guard: {len(all_mappings)} changes exceeds MAX_CHANGES_PER_RUN={max_changes}")
        return {**state, "validation_errors": errors}

    # 4. Auditor LLM re-confirmation
    from aiac.agent.roles.role.prompts import AUDITOR_SYSTEM

    try:
        llm = ChatOpenAI(model=os.getenv("OPENAI_MODEL_NAME", "gpt-4o-mini"), temperature=0)
        verdict: ValidationVerdict = llm.with_structured_output(ValidationVerdict).invoke(
            [
                {"role": "system", "content": AUDITOR_SYSTEM},
                {"role": "user", "content": f"Proposed diff: {diff.model_dump_json()}"},
            ]
        )
    except Exception as exc:
        raise HTTPException(status_code=504, detail=f"LLM unavailable during audit: {exc}") from exc

    if not verdict.approved:
        errors.append(f"Auditor rejected: {verdict.reason}")

    return {**state, "validation_errors": errors}


def apply_mappings(state: BaseAgentState) -> dict:
    if state.get("validation_errors"):
        return state

    realm = state["realm"]
    diff: ProposedDiff = state["proposed_diff"]

    try:
        policy = Policy.for_realm(realm)
        if diff.add:
            policy.add_role_composites(
                diff.add[0].role_name,
                [m.model_dump() for m in diff.add],
            )
        if diff.remove:
            policy.remove_role_composites(
                diff.remove[0].role_name,
                [m.model_dump() for m in diff.remove],
            )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Policy Service unavailable: {exc}") from exc

    return {**state, "added": list(diff.add), "removed": list(diff.remove)}


def format_response(state: BaseAgentState) -> dict:
    added = state.get("added", [])
    removed = state.get("removed", [])
    errors = state.get("validation_errors", [])

    if errors:
        summary = f"Validation failed: {'; '.join(errors)}"
    else:
        summary = f"Applied {len(added)} additions and {len(removed)} removals."

    return {**state, "summary": summary, "provisioned": None}
