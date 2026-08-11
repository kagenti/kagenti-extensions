"""Rego package generation for the PDP Policy Writer (OPA).

Translates an ``AgentPolicyModel`` into the two fixed-package Rego strings the
live AuthBridge embedded OPA plugin evaluates.

Both packages use **fixed** names — ``authbridge.client.inbound.request`` and
``authbridge.client.outbound.request`` (each ``import rego.v1``). Per-agent
isolation is at the CR/bundle level (the bundle-service looks a CR up by
namespace+name), never in the package name. The bundle-service combiner requires
the exact path ``data.authbridge.client.<tier>``, so no slug ever appears in the
package name.

The Rego ``input`` follows the live plugin shape:

- ``input.identity.subject`` — the delegated user (``sub`` claim).
- ``input.identity.client_id`` — the calling client (inbound) / the agent's own
  client (outbound).
- ``input.identity.service_id`` — the downstream target audience the exchanged
  token was minted for (a full SPIFFE ID); outbound only.
- ``input.mcp.params.name`` — the **bare** invoked MCP tool name (e.g.
  ``source-read``); outbound only. A missing ``params.name`` (e.g. ``tools/list``)
  or an absent ``service_id`` matches nothing and is therefore denied.
"""

import json
import re

from aiac.policy.model.models import AgentPolicyModel, PolicyRule

__all__ = ["identity_ref", "generate_inbound_rego", "generate_outbound_rego"]

_SPIFFE_RE = re.compile(r"^spiffe://[^/]+/ns/(?P<ns>[^/]+)/sa/(?P<name>[^/]+)$")

# A DNS-1123 label: lowercase alphanumerics and '-', starting/ending alphanumeric.
_DNS1123_LABEL_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")


def _valid_label(segment: str) -> bool:
    """True when ``segment`` is a valid DNS-1123 label (<=63 chars)."""
    return len(segment) <= 63 and _DNS1123_LABEL_RE.fullmatch(segment) is not None


def identity_ref(agent_id: str) -> tuple[str, str]:
    """``(namespace, name)`` for CR placement.

    Accepts a SPIFFE URI (``spiffe://<trust-domain>/ns/<ns>/sa/<name>``) or a
    plain ``<ns>/<name>``. Both segments are validated as DNS-1123 labels
    (``^[a-z0-9]([-a-z0-9]*[a-z0-9])?$``, <=63 chars).

    Raises ``ValueError`` when no namespace is derivable (e.g. a bare
    ``github-agent`` with no ``/``) or either segment is not a valid label —
    there is **no** fallback.
    """
    match = _SPIFFE_RE.match(agent_id)
    if match:
        namespace, name = match["ns"], match["name"]
    else:
        parts = agent_id.split("/")
        if len(parts) != 2:
            raise ValueError(
                f"agent_id {agent_id!r} has no derivable namespace (expected "
                "SPIFFE spiffe://td/ns/<ns>/sa/<name> or plain <ns>/<name>)"
            )
        namespace, name = parts
    if not _valid_label(namespace) or not _valid_label(name):
        raise ValueError(
            f"agent_id {agent_id!r} yields invalid DNS-1123 label(s): "
            f"namespace={namespace!r}, name={name!r}"
        )
    return namespace, name


def _render_list(var: str, values: list[str]) -> str:
    """Render ``{var} := ["a", "b"]`` as Rego (empty-safe: ``[]``).

    Each value is emitted via ``json.dumps`` so quotes/newlines/backslashes are escaped —
    Rego string syntax is JSON-compatible, and this prevents Rego injection / broken output."""
    inner = ", ".join(json.dumps(v) for v in values)
    return f"{var} := [{inner}]"


def _render_map(var: str, mapping: dict[str, list[str]]) -> str:
    """Render ``{var} := { "key": ["a", "b"], ... }`` as Rego (empty-safe: ``{}``).

    Keys and values are emitted via ``json.dumps`` so quotes/newlines/backslashes are escaped
    (JSON-compatible Rego string syntax) — this prevents Rego injection / broken output."""
    if not mapping:
        return f"{var} := {{}}"
    lines = [f"{var} := {{"]
    for key, values in mapping.items():
        inner = ", ".join(json.dumps(v) for v in values)
        lines.append(f"    {json.dumps(key)}: [{inner}],")
    lines.append("}")
    return "\n".join(lines)


def _deprefix(scope) -> str:
    """De-prefix an outbound scope value to the bare MCP tool name.

    Provisioned scope names are prefixed with their owning workload
    (``github-tool.source-read``, ``github-agent.source_operations``), but the
    value that arrives in ``input.mcp.params.name`` at runtime is the **bare**
    tool name (``source-read``). Strip a leading ``"<owner>."`` where ``owner =
    identity_ref(scope.serviceId).name``.

    Fall back to ``scope.name`` unchanged when ``serviceId`` is missing /
    underivable or the ``"<owner>."`` prefix is not present — no partial strip.
    """
    service_id = getattr(scope, "serviceId", "") or ""
    if service_id:
        try:
            _, owner = identity_ref(service_id)
        except ValueError:
            return scope.name
        prefix = f"{owner}."
        if scope.name.startswith(prefix):
            return scope.name[len(prefix) :]
    return scope.name


def _group_rules(rules: list[PolicyRule]) -> dict[str, list[str]]:
    """Group rules into ``{role.name: [scope.name, ...]}`` preserving first-seen order."""
    grouped: dict[str, list[str]] = {}
    for rule in rules:
        scopes = grouped.setdefault(rule.role.name, [])
        if rule.scope.name not in scopes:
            scopes.append(rule.scope.name)
    return grouped


def _group_rules_deprefixed(rules: list[PolicyRule]) -> dict[str, list[str]]:
    """Like ``_group_rules`` but de-prefixes each scope value (outbound only).

    Groups ``{role.name: [_deprefix(scope), ...]}`` — used for
    ``subject_role_scopes`` / ``agent_role_scopes``, whose values must match the
    bare ``input.mcp.params.name``."""
    grouped: dict[str, list[str]] = {}
    for rule in rules:
        scopes = grouped.setdefault(rule.role.name, [])
        value = _deprefix(rule.scope)
        if value not in scopes:
            scopes.append(value)
    return grouped


def _names(items) -> list[str]:
    """Extract the ``.name`` of each entity in a list."""
    return [item.name for item in items]


def _name_map(mapping) -> dict[str, list[str]]:
    """Turn ``{id: [entity, ...]}`` into ``{id: [entity.name, ...]}``."""
    return {key: _names(values) for key, values in mapping.items()}


def _name_map_deprefixed(mapping) -> dict[str, list[str]]:
    """Like ``_name_map`` but de-prefixes each value (outbound ``target_scopes``).

    Keys stay the **full** target service id (they match
    ``input.identity.service_id``, a full SPIFFE ID); only the scope *values*
    de-prefix to the bare MCP tool names carried in ``input.mcp.params.name``."""
    return {
        key: [_deprefix(scope) for scope in scopes]
        for key, scopes in mapping.items()
    }


# The inbound subject gate: the subject holds a role that grants at least one of
# the agent's own scopes (compared internally against agent_scopes, using FULL
# scope names — never against input.mcp.params.name).
_INBOUND_SUBJECT_OK = (
    "subject_ok if {\n"
    "    some role in subject_roles[input.identity.subject]\n"
    "    some scope in role_scopes[role]\n"
    "    scope in agent_scopes\n"
    "}"
)

# The outbound subject gate: the delegated user's role admits the invoked tool
# (bare input.mcp.params.name is in that role's de-prefixed subject_role_scopes).
_OUTBOUND_SUBJECT_OK = (
    "subject_ok if {\n"
    "    some role in subject_roles[input.identity.subject]\n"
    "    input.mcp.params.name in subject_role_scopes[role]\n"
    "}"
)

# The outbound capability gate: the target service (keyed by its full SPIFFE id)
# admits the invoked tool. This — not agent_role_scopes — is the capability gate.
_OUTBOUND_TARGET_OK = (
    "target_ok if {\n"
    "    input.mcp.params.name in target_scopes[input.identity.service_id]\n"
    "}"
)


def generate_inbound_rego(
    model: AgentPolicyModel, platform_clients: tuple[str, ...] = ("rossoctl",)
) -> str:
    """Render the fixed ``authbridge.client.inbound.request`` Rego package.

    Gates a caller reaching the agent. ``allow`` requires ``subject_ok`` (the
    subject holds a role granting >=1 of ``agent_scopes``) AND ``source_ok``.

    ``source_ok`` passes when there is no calling ``client_id`` (end-user
    traffic), when the ``client_id`` is one of ``platform_clients`` (the
    mandatory bypass — one ``source_ok if { input.identity.client_id == "<c>" }``
    rule per client; without it end-user traffic, which carries the platform
    client, would be denied), or when that client holds a role granting an agent
    scope. Inbound values are **not** de-prefixed — the gate compares scopes
    internally, never against ``input.mcp.params.name``.
    """
    source_ok_rules = ["source_ok if { not input.identity.client_id }"]
    for client in platform_clients:
        source_ok_rules.append(
            f"source_ok if {{ input.identity.client_id == {json.dumps(client)} }}"
        )
    source_ok_rules.append(
        "source_ok if {\n"
        "    some role in source_roles[input.identity.client_id]\n"
        "    some scope in role_scopes[role]\n"
        "    scope in agent_scopes\n"
        "}"
    )
    declarations = "\n".join(
        [
            _render_map("subject_roles", _name_map(model.subject_roles)),
            _render_map("source_roles", _name_map(model.source_roles)),
            _render_map("role_scopes", _group_rules(model.inbound_rules)),
        ]
    )
    rules = "\n".join(
        [_INBOUND_SUBJECT_OK]
        + source_ok_rules
        + ["default allow := false\nallow if { subject_ok; source_ok }"]
    )
    parts = [
        "package authbridge.client.inbound.request\nimport rego.v1",
        _render_list("agent_scopes", _names(model.agent_scopes)),
        declarations,
        rules,
    ]
    return "\n\n".join(parts) + "\n"


def generate_outbound_rego(model: AgentPolicyModel) -> str:
    """Render the fixed ``authbridge.client.outbound.request`` Rego package.

    Gates the agent's token-exchanged call to a downstream target, per invoked
    tool. ``allow`` is an AND on the **same** ``input.mcp.params.name``:
    ``subject_ok`` (the delegated user's role admits the tool, via de-prefixed
    ``subject_role_scopes``) AND ``target_ok`` (the target service — keyed by the
    full ``input.identity.service_id`` SPIFFE id — admits the tool, via
    de-prefixed ``target_scopes`` values).

    ``agent_roles`` / ``agent_role_scopes`` are emitted for debugging but are
    **not** referenced by ``allow`` — ``target_scopes[input.identity.service_id]``
    already *is* the capability gate. This package emits neither ``agent_scopes``
    nor the inbound ``role_scopes`` gate.
    """
    declarations = "\n".join(
        [
            _render_list("agent_roles", _names(model.agent_roles)),
            _render_map("subject_roles", _name_map(model.subject_roles)),
            _render_map(
                "subject_role_scopes",
                _group_rules_deprefixed(model.outbound_subject_rules),
            ),
            _render_map(
                "agent_role_scopes", _group_rules_deprefixed(model.outbound_rules)
            ),
            _render_map("target_scopes", _name_map_deprefixed(model.target_scopes)),
        ]
    )
    rules = "\n".join(
        [
            _OUTBOUND_SUBJECT_OK,
            _OUTBOUND_TARGET_OK,
            "default allow := false\nallow if { subject_ok; target_ok }",
        ]
    )
    parts = [
        "package authbridge.client.outbound.request\nimport rego.v1",
        declarations,
        rules,
    ]
    return "\n\n".join(parts) + "\n"
