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

**ALLOW/DENY (deny-overrides).** Each gate is emitted twice — an ``*_allow_ok``
gate driven by the ALLOW scope maps and a symmetric ``*_deny_ok`` gate driven by
the DENY scope maps. A request is permitted iff every ALLOW gate passes and no
DENY gate matches::

    # inbound
    allow if { subject_allow_ok; source_allow_ok; not subject_deny_ok; not source_deny_ok }
    # outbound
    allow if { subject_allow_ok; target_allow_ok; not subject_deny_ok; not target_deny_ok }

The identity maps (``subject_roles`` / ``source_roles``) are **effect-agnostic**,
so a principal that appears only in a DENY rule still resolves and its
prohibition fires.
"""

import json
import re

from aiac.policy.model.models import AgentPolicyModel, PolicyRule, RuleEffect

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

    Groups ``{role.name: [_deprefix(scope), ...]}`` — used for the outbound
    ``subject_role_allow_scopes`` / ``subject_role_deny_scopes`` /
    ``agent_role_scopes`` maps, whose values must match the bare
    ``input.mcp.params.name``."""
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
    """Like ``_name_map`` but de-prefixes each value (outbound ``target_*_scopes``).

    Keys stay the **full** target service id (they match
    ``input.identity.service_id``, a full SPIFFE ID); only the scope *values*
    de-prefix to the bare MCP tool names carried in ``input.mcp.params.name``."""
    return {
        key: [_deprefix(scope) for scope in scopes]
        for key, scopes in mapping.items()
    }


# --- inbound gate templates -------------------------------------------------
#
# The inbound subject gate is emitted twice against the SAME shape: an
# ``*_allow_ok`` gate reads the ALLOW scope map, a symmetric ``*_deny_ok`` gate
# reads the DENY scope map. Both require the matched scope to be one of the
# agent's own ``agent_scopes`` (the inbound audience), compared internally with
# FULL scope names — never against ``input.mcp.params.name``. A subject/source
# that only appears in a DENY rule still resolves because the identity maps
# (``subject_roles`` / ``source_roles``) are effect-agnostic.


def _inbound_subject_gate(gate: str, scope_map: str) -> str:
    return (
        f"{gate} if {{\n"
        "    some role in subject_roles[input.identity.subject]\n"
        f"    some scope in {scope_map}[role]\n"
        "    scope in agent_scopes\n"
        "}"
    )


def _inbound_source_allow_gate(platform_clients: tuple[str, ...]) -> str:
    """The inbound source ALLOW gate.

    Passes when there is no calling ``client_id`` (end-user traffic), when the
    ``client_id`` is one of ``platform_clients`` (the mandatory bypass — one rule
    per client; without it end-user traffic, which carries the platform client,
    would be denied), or when that client holds a role granting an agent scope.
    """
    rules = ["source_allow_ok if { not input.identity.client_id }"]
    for client in platform_clients:
        rules.append(
            f"source_allow_ok if {{ input.identity.client_id == {json.dumps(client)} }}"
        )
    rules.append(
        "source_allow_ok if {\n"
        "    some role in source_roles[input.identity.client_id]\n"
        "    some scope in source_role_allow_scopes[role]\n"
        "    scope in agent_scopes\n"
        "}"
    )
    return "\n".join(rules)


def _inbound_source_deny_gate() -> str:
    """The inbound source DENY gate.

    An absent client_id (or a platform client) has no roles here, so this gate
    simply never fires for it — the ALLOW-side bypass is not undone by a deny.
    """
    return (
        "source_deny_ok if {\n"
        "    some role in source_roles[input.identity.client_id]\n"
        "    some scope in source_role_deny_scopes[role]\n"
        "    scope in agent_scopes\n"
        "}"
    )


# --- outbound gate templates ------------------------------------------------
#
# The outbound decision is a per-tool two-gate AND, both keyed on the invoked
# tool ``input.mcp.params.name`` (the delegated user reaching a downstream
# target):
#   subject gate    — the delegated user's role admits the invoked tool
#   capability gate — the target service admits the invoked tool
# Each gate is emitted twice (allow/deny). ``allow`` is deny-overrides: both
# ALLOW gates pass on the invoked tool and neither DENY gate matches it.


def _outbound_subject_gate(gate: str, scope_map: str) -> str:
    return (
        f"{gate} if {{\n"
        "    some role in subject_roles[input.identity.subject]\n"
        f"    input.mcp.params.name in {scope_map}[role]\n"
        "}"
    )


def _outbound_target_gate(gate: str, scope_map: str) -> str:
    return (
        f"{gate} if {{\n"
        f"    input.mcp.params.name in {scope_map}[input.identity.service_id]\n"
        "}"
    )


# --- trailing decision block (the only thing default_effect changes) --------
#
# CRITICAL: the generator assumes disjoint ALLOW/DENY per (role, scope). A
# genuine grant/deny overlap on the same pair is an upstream policy conflict
# surfaced as HTTP 422 (PRB ``PolicyContradictionError``) and is NEVER
# reconciled here. The ``allow := false if { <deny> }`` rules below are not
# conflict reconciliation: they give an explicit deny precedence over a
# permissive default, and resolve co-occurring-but-disjoint denies at request
# time (a subject holding multiple roles; the outbound two-gate decision) —
# each individual (role, scope) stays allow-XOR-deny.


def _decision_block(
    default_effect: RuleEffect, allow_body: str, deny_gates: tuple[str, ...]
) -> str:
    """Render the trailing ``allow`` decision — the *only* part that varies by mode.

    ``DENY`` (least-privilege) reproduces today's output byte-for-byte:
    ``default allow := false`` plus the single ``allow if { <allow_body> }`` rule
    (an allow-conjunction with inline ``not …_deny_ok`` guards).

    ``ALLOW`` opens the default and lets explicit denies override: ``default
    allow := true`` plus one ``allow := false if { <gate> }`` rule per deny gate.
    A literal flip of the constant alone is insufficient — an incremental
    ``allow if { … }`` body can only push ``allow`` toward ``true``, so the deny
    guards must become separate ``allow := false if`` rules to pull it back down
    (deny-overrides over a permissive default)."""
    if default_effect == RuleEffect.ALLOW:
        lines = ["default allow := true"]
        lines += [f"allow := false if {{ {gate} }}" for gate in deny_gates]
        return "\n".join(lines)
    return "default allow := false\n" + f"allow if {{ {allow_body} }}"


def generate_inbound_rego(
    model: AgentPolicyModel, platform_clients: tuple[str, ...] = ("rossoctl",)
) -> str:
    """Render the fixed ``authbridge.client.inbound.request`` Rego package.

    Gates a caller reaching the agent. The decision is deny-overrides:
    ``allow`` requires ``subject_allow_ok`` (the subject holds a role granting
    >=1 of ``agent_scopes`` via the ALLOW map) AND ``source_allow_ok``, and
    fires only when neither ``subject_deny_ok`` nor ``source_deny_ok`` matches.

    ``source_allow_ok`` passes when there is no calling ``client_id`` (end-user
    traffic), when the ``client_id`` is one of ``platform_clients`` (the
    mandatory bypass), or when that client holds a role granting an agent scope.
    Inbound values are **not** de-prefixed — the gates compare scopes internally
    against ``agent_scopes``, never against ``input.mcp.params.name``.
    """
    declarations = "\n".join(
        [
            _render_map("subject_roles", _name_map(model.subject_roles)),
            _render_map("source_roles", _name_map(model.source_roles)),
            _render_map(
                "subject_role_allow_scopes",
                _group_rules(model.inbound_subject_allow_rules),
            ),
            _render_map(
                "subject_role_deny_scopes",
                _group_rules(model.inbound_subject_deny_rules),
            ),
            _render_map(
                "source_role_allow_scopes",
                _group_rules(model.inbound_source_allow_rules),
            ),
            _render_map(
                "source_role_deny_scopes",
                _group_rules(model.inbound_source_deny_rules),
            ),
        ]
    )
    rules = "\n".join(
        [
            _inbound_subject_gate("subject_allow_ok", "subject_role_allow_scopes"),
            _inbound_subject_gate("subject_deny_ok", "subject_role_deny_scopes"),
            _inbound_source_allow_gate(platform_clients),
            _inbound_source_deny_gate(),
            # Branch ONLY the trailing decision block on model.default_effect. Under
            # ALLOW the allow gates / allow scope maps above are inert-but-emitted
            # (kept for structural symmetry and downstream tooling); the decision
            # is deny-if-either-side.
            _decision_block(
                model.default_effect,
                "subject_allow_ok; source_allow_ok; "
                "not subject_deny_ok; not source_deny_ok",
                ("subject_deny_ok", "source_deny_ok"),
            ),
        ]
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
    tool. The decision is deny-overrides on the **same** ``input.mcp.params.name``:
    ``allow`` requires ``subject_allow_ok`` (the delegated user's role admits the
    tool, via de-prefixed ``subject_role_allow_scopes``) AND ``target_allow_ok``
    (the target service — keyed by the full ``input.identity.service_id`` SPIFFE
    id — admits the tool, via de-prefixed ``target_allow_scopes``), and fires only
    when neither ``subject_deny_ok`` nor ``target_deny_ok`` matches.

    ``agent_roles`` / ``agent_role_scopes`` are emitted for debugging but are
    **not** referenced by ``allow`` — ``target_allow_scopes[input.identity.service_id]``
    already *is* the capability gate. This package emits neither ``agent_scopes``
    nor the inbound scope gates.
    """
    declarations = "\n".join(
        [
            _render_list("agent_roles", _names(model.agent_roles)),
            _render_map("subject_roles", _name_map(model.subject_roles)),
            _render_map(
                "subject_role_allow_scopes",
                _group_rules_deprefixed(model.outbound_subject_allow_rules),
            ),
            _render_map(
                "subject_role_deny_scopes",
                _group_rules_deprefixed(model.outbound_subject_deny_rules),
            ),
            # agent_role_scopes is emitted for debugging/observability only; the
            # allow decision never references it (target_allow_scopes is the
            # capability gate). The leading Rego comment says so in the bundle.
            "# informational/debugging only — not referenced by allow\n"
            + _render_map(
                "agent_role_scopes",
                _group_rules_deprefixed(model.outbound_target_allow_rules),
            ),
            _render_map(
                "target_allow_scopes", _name_map_deprefixed(model.target_allow_scopes)
            ),
            _render_map(
                "target_deny_scopes", _name_map_deprefixed(model.target_deny_scopes)
            ),
        ]
    )
    rules = "\n".join(
        [
            _outbound_subject_gate("subject_allow_ok", "subject_role_allow_scopes"),
            _outbound_subject_gate("subject_deny_ok", "subject_role_deny_scopes"),
            _outbound_target_gate("target_allow_ok", "target_allow_scopes"),
            _outbound_target_gate("target_deny_ok", "target_deny_scopes"),
            # Branch ONLY the trailing decision block on model.default_effect.
            # Under ALLOW this drops the old subject_allow_ok AND target_allow_ok
            # conjunction (deny-if-either-side): a negated allow-gate AND would
            # wrongly DENY every unmentioned pair. An unmentioned (role, tool)
            # pair falls through to the permissive default; an explicit deny on
            # EITHER gate overrides it.
            _decision_block(
                model.default_effect,
                "subject_allow_ok; target_allow_ok; "
                "not subject_deny_ok; not target_deny_ok",
                ("subject_deny_ok", "target_deny_ok"),
            ),
        ]
    )
    parts = [
        "package authbridge.client.outbound.request\nimport rego.v1",
        declarations,
        rules,
    ]
    return "\n\n".join(parts) + "\n"
