"""Idempotent Keycloak setup for the RFC 8693 token exchange this demo's ``run-*.py`` scripts
perform: an ROPC client for the demo users, and the exchange enablement on the agent client.

Every step is wrapped so an already-satisfied condition prints a note rather than aborting — this
demo runs against both the freshly-installed cluster (where none of this exists yet) and the
already-configured one this was developed against (where all of it is already true).

Not a standalone script — imported by ``03-setup.py``, its sole caller (unlike ``scenario.py``/
``_lib.py``, which are shared across ``init/``/``onboard/``/``run/`` and live in ``lib/``).
"""

from __future__ import annotations

import scenario as scn
from _lib import Config, note, ok


def ensure_ropc_client(admin, cfg: Config) -> None:
    """Public client with direct-access-grants enabled, so ``grant_type=password`` needs no client
    secret. Full-scope-allowed (the admin API default for a freshly created client) is what makes
    the demo users' access tokens carry ``realm_access.roles`` and the tool audience — a client
    created with a narrower default would silently produce tokens this demo can't drive with."""
    admin.change_current_realm(cfg.realm)
    if admin.get_client_id(scn.ROPC_CLIENT_ID) is not None:
        note(f"ROPC client {scn.ROPC_CLIENT_ID!r} already exists")
        return
    admin.create_client({
        "clientId": scn.ROPC_CLIENT_ID,
        "publicClient": True,
        "directAccessGrantsEnabled": True,
        "standardFlowEnabled": False,
        "enabled": True,
    })
    ok(f"created ROPC client {scn.ROPC_CLIENT_ID!r}")


def ensure_token_exchange_enabled(admin, cfg: Config, client_uuid: str, client_name: str) -> None:
    """Set ``standard.token.exchange.enabled=true`` on the agent client — the modern (Keycloak 26)
    RFC 8693 enablement, a client attribute rather than an authorization-services permission."""
    admin.change_current_realm(cfg.realm)
    client = admin.get_client(client_uuid)
    attrs = client.get("attributes", {})
    if attrs.get("standard.token.exchange.enabled") == "true":
        note(f"token exchange already enabled on {client_name!r}")
        return
    attrs["standard.token.exchange.enabled"] = "true"
    # update_client issues a PUT that replaces the whole client representation, so send the full
    # fetched client with only ``attributes`` overridden — a bare {"attributes": ...} would clobber
    # the client's other fields.
    admin.update_client(client_uuid, {**client, "attributes": attrs})
    ok(f"enabled standard.token.exchange.enabled on {client_name!r}")


def ensure_default_audience_scope(admin, cfg: Config, client_uuid: str, client_name: str, scope_name: str) -> bool:
    """Ensure ``scope_name`` (the tool's ``*-aud`` audience client scope) is a DEFAULT scope on the
    agent client, so an exchanged token's ``aud`` includes the tool without the caller requesting it
    explicitly.

    Returns ``True`` when the scope is now assigned (added here or already present) and ``False``
    when the scope does not exist yet, so the scope was skipped. The ``03-setup.py`` path calls this
    before the tool is onboarded, where ``False`` is expected; the ``05-onboard-tool.py`` path calls
    it afterwards, where ``False`` means the token-exchange audience is missing and onboarding must
    not report success."""
    admin.change_current_realm(cfg.realm)
    scope = admin.get_client_scope_by_name(scope_name)
    if scope is None:
        note(f"client scope {scope_name!r} does not exist yet (tool not onboarded?) — skipping")
        return False
    existing = {s["name"] for s in admin.get_client_default_client_scopes(client_uuid)}
    if scope_name in existing:
        note(f"{scope_name!r} already a default scope on {client_name!r}")
        return True
    admin.add_client_default_client_scope(client_uuid, scope["id"], {})
    ok(f"added {scope_name!r} as a default scope on {client_name!r}")
    return True


def run(admin, cfg: Config, *, agent_uuid: str) -> None:
    ensure_ropc_client(admin, cfg)
    ensure_token_exchange_enabled(admin, cfg, agent_uuid, scn.AGENT_WORKLOAD)
    tool_aud_scope = f"agent-{cfg.namespace}-{scn.TOOL_WORKLOAD}-aud"
    ensure_default_audience_scope(admin, cfg, agent_uuid, scn.AGENT_WORKLOAD, tool_aud_scope)
