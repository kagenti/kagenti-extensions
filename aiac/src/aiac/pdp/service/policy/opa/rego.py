"""Rego package generation for the PDP Policy Writer (OPA).

Translates an ``AgentPolicyModel`` into Rego package strings ready to be
written to disk by the PDP Policy Writer.
"""

from aiac.policy.model.models import AgentPolicyModel

__all__ = ["slugify", "generate_inbound_rego", "generate_outbound_rego"]


def slugify(agent_id: str) -> str:
    """Turn an agent id into a valid Rego package name segment."""
    return agent_id.replace("-", "_").lower()


def _render_name_map(var: str, mapping: dict[str, list[str]]) -> str:
    """Render ``{var} := { "key": ["a", "b"], ... }`` as Rego."""
    lines = [f"{var} := {{"]
    for key, values in mapping.items():
        rendered = ", ".join(f'"{v}"' for v in values)
        lines.append(f'    "{key}": [{rendered}],')
    lines.append("}")
    return "\n".join(lines)


def generate_inbound_rego(model: AgentPolicyModel) -> str:
    """Render the ``authz.{slug}.inbound`` Rego package for an agent."""
    slug = slugify(model.agent_id)
    source_roles = {
        source: [role.name for role in roles]
        for source, roles in model.source_roles.items()
    }
    parts = [
        f"package authz.{slug}.inbound",
        "default allow := false",
        _render_name_map("source_roles", source_roles),
    ]
    for rule in model.inbound_rules:
        parts.append(
            "allow if {\n"
            "    some role in source_roles[input.source]\n"
            f'    role == "{rule.role.name}"\n'
            f'    input.scope == "{rule.scope.name}"\n'
            "}"
        )
    return "\n\n".join(parts) + "\n"


def generate_outbound_rego(model: AgentPolicyModel) -> str:
    """Render the ``authz.{slug}.outbound`` Rego package for an agent."""
    slug = slugify(model.agent_id)
    target_scopes = {
        target: [scope.name for scope in scopes]
        for target, scopes in model.target_scopes.items()
    }
    parts = [
        f"package authz.{slug}.outbound",
        "default allow := false",
        _render_name_map("target_scopes", target_scopes),
    ]
    for rule in model.outbound_rules:
        parts.append(
            "allow if {\n"
            f'    input.role == "{rule.role.name}"\n'
            f'    input.scope == "{rule.scope.name}"\n'
            f'    "{rule.scope.name}" in target_scopes[input.target]\n'
            "}"
        )
    return "\n\n".join(parts) + "\n"
