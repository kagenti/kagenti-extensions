"""Rego package generation for the PDP Policy Writer (OPA).

Translates an ``AgentPolicyModel`` into Rego package strings ready to be
written to disk by the PDP Policy Writer.

The generated packages are **ID-only**: the Rego ``input`` carries only
``{subject, source, target}`` identifiers. All role/scope mappings are
embedded in the package, and ``allow`` resolves IDs -> roles -> scopes
internally.
"""

import re

from aiac.policy.model.models import AgentPolicyModel, PolicyRule

__all__ = ["slugify", "generate_inbound_rego", "generate_outbound_rego"]

_SPIFFE_RE = re.compile(r"^spiffe://[^/]+/ns/(?P<ns>[^/]+)/sa/(?P<name>[^/]+)$")


def _short_id(agent_id: str) -> str:
    """Reduce a clientId to ``{namespace}/{name}``, dropping the SPIFFE trust domain.

    Under SPIRE, agent_id is a SPIFFE URI (``spiffe://host/ns/{ns}/sa/{name}``); without
    SPIRE it's already ``{ns}/{name}``. Either way the trust domain/host is not part of a
    stable identity, so the slug must not depend on it.
    """
    match = _SPIFFE_RE.match(agent_id)
    return f"{match['ns']}/{match['name']}" if match else agent_id


def slugify(agent_id: str) -> str:
    """Turn an agent id into a valid Rego package name segment / filename.

    Predictable regardless of whether SPIRE is enabled: derived from ``{ns}/{name}``,
    not the full slash/colon-bearing clientId or SPIFFE URI.
    """
    return re.sub(r"[^a-z0-9]+", "_", _short_id(agent_id).lower()).strip("_")


def _render_list(var: str, values: list[str]) -> str:
    """Render ``{var} := ["a", "b"]`` as Rego (empty-safe: ``[]``)."""
    inner = ", ".join(f'"{v}"' for v in values)
    return f"{var} := [{inner}]"


def _render_map(var: str, mapping: dict[str, list[str]]) -> str:
    """Render ``{var} := { "key": ["a", "b"], ... }`` as Rego (empty-safe: ``{}``)."""
    if not mapping:
        return f"{var} := {{}}"
    lines = [f"{var} := {{"]
    for key, values in mapping.items():
        inner = ", ".join(f'"{v}"' for v in values)
        lines.append(f'    "{key}": [{inner}],')
    lines.append("}")
    return "\n".join(lines)


def _group_rules(rules: list[PolicyRule]) -> dict[str, list[str]]:
    """Group rules into ``{role.name: [scope.name, ...]}`` preserving first-seen order."""
    grouped: dict[str, list[str]] = {}
    for rule in rules:
        scopes = grouped.setdefault(rule.role.name, [])
        if rule.scope.name not in scopes:
            scopes.append(rule.scope.name)
    return grouped


def _names(items) -> list[str]:
    """Extract the ``.name`` of each entity in a list."""
    return [item.name for item in items]


def _name_map(mapping) -> dict[str, list[str]]:
    """Turn ``{id: [entity, ...]}`` into ``{id: [entity.name, ...]}``."""
    return {key: _names(values) for key, values in mapping.items()}


# The subject gate is identical in both packages: the subject holds a role
# that grants at least one of the agent's own scopes.
_SUBJECT_OK = (
    "subject_ok if {\n"
    "    some role in subject_roles[input.subject]\n"
    "    some scope in role_scopes[role]\n"
    "    scope in agent_scopes\n"
    "}"
)


def generate_inbound_rego(model: AgentPolicyModel) -> str:
    """Render the ``authz.{slug}.inbound`` Rego package for an agent.

    Input is ``{subject, source}`` (ids only). ``subject`` is mandatory;
    ``source`` is optional (absent source passes). The gate is coarse: it
    passes when the principal holds a role granting >=1 of ``agent_scopes``.
    """
    slug = slugify(model.agent_id)
    parts = [
        f"package authz.{slug}.inbound",
        _render_list("agent_scopes", _names(model.agent_scopes)),
        _render_map("subject_roles", _name_map(model.subject_roles)),
        _render_map("source_roles", _name_map(model.source_roles)),
        _render_map("role_scopes", _group_rules(model.inbound_rules)),
        _SUBJECT_OK,
        (
            "source_ok if { not input.source }\n"
            "source_ok if {\n"
            "    some role in source_roles[input.source]\n"
            "    some scope in role_scopes[role]\n"
            "    scope in agent_scopes\n"
            "}"
        ),
        "default allow := false\nallow if { subject_ok; source_ok }",
    ]
    return "\n\n".join(parts) + "\n"


# The outbound subject gate is user->tool (distinct from inbound's user->agent):
# the subject holds a role that grants at least one tool scope the target accepts.
_OUTBOUND_SUBJECT_OK = (
    "subject_ok if {\n"
    "    some role in subject_roles[input.subject]\n"
    "    some scope in outbound_subject_role_scopes[role]\n"
    "    scope in target_scopes[input.target]\n"
    "}"
)


def generate_outbound_rego(model: AgentPolicyModel) -> str:
    """Render the ``authz.{slug}.outbound`` Rego package for an agent.

    Input is ``{subject, target}`` (ids only). Both must pass: the subject
    holds a role granting >=1 tool scope the ``target`` accepts (user->tool, via
    ``outbound_subject_role_scopes`` from ``outbound_subject_rules``), AND the
    agent (via ``agent_roles``) is permitted >=1 scope the ``target`` accepts.
    ``target_scopes`` is used directly (target id -> scopes) -- no inversion.
    """
    slug = slugify(model.agent_id)
    parts = [
        f"package authz.{slug}.outbound",
        _render_list("agent_roles", _names(model.agent_roles)),
        _render_list("agent_scopes", _names(model.agent_scopes)),
        _render_map("subject_roles", _name_map(model.subject_roles)),
        _render_map(
            "outbound_subject_role_scopes", _group_rules(model.outbound_subject_rules)
        ),
        _render_map("agent_role_scopes", _group_rules(model.outbound_rules)),
        _render_map("target_scopes", _name_map(model.target_scopes)),
        _OUTBOUND_SUBJECT_OK,
        (
            "target_ok if {\n"
            "    some role in agent_roles\n"
            "    some scope in agent_role_scopes[role]\n"
            "    scope in target_scopes[input.target]\n"
            "}"
        ),
        "default allow := false\nallow if { subject_ok; target_ok }",
    ]
    return "\n\n".join(parts) + "\n"
