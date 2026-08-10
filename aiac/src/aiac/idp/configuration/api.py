import os
from pathlib import Path
from typing import Protocol

import requests
from dotenv import load_dotenv

from aiac.idp.configuration.models import Role, Scope, Service, ServiceType, Subject
from aiac.shared.upstream import run_upstream


class _NamedDefinition(Protocol):
    """Structural type for a not-yet-persisted role/scope: just a name + description.
    Lets ``create_service_role`` / ``create_service_scope`` accept the agent layer's
    ``RoleDefinition`` / ``ScopeDefinition`` without the IdP library importing the agent."""

    name: str
    description: str

load_dotenv(Path(__file__).resolve().parent / ".env")


# Single source of truth for the Keycloak realm the whole AIAC pipeline operates on. Provisioning,
# the Service Policy Builder, and the Policy Computation Engine all resolve the realm through
# ``Configuration.for_default_realm()`` so they can never diverge onto different env vars.
REALM_ENV_VAR = "KEYCLOAK_REALM"


class Configuration:
    def __init__(self, realm: str) -> None:
        self.realm = realm

    @classmethod
    def for_realm(cls, realm: str) -> "Configuration":
        return cls(realm)

    @classmethod
    def for_default_realm(cls) -> "Configuration":
        """Build a ``Configuration`` for the realm named by ``$KEYCLOAK_REALM`` — the single source
        of truth shared by provisioning, the policy builder, and the computation engine.

        Fails fast if the env var is unset or empty: an empty realm would silently target the
        wrong Keycloak realm rather than surface the misconfiguration."""
        realm = os.getenv(REALM_ENV_VAR, "").strip()
        if not realm:
            raise RuntimeError(
                f"{REALM_ENV_VAR} is unset or empty; set it to the Keycloak realm the AIAC "
                "pipeline operates on"
            )
        return cls.for_realm(realm)

    def _base_url(self) -> str:
        return os.getenv("AIAC_PDP_CONFIG_URL", "http://127.0.0.1:7071")

    def _params(self) -> dict[str, str]:
        return {"realm": self.realm}

    def _check(self, resp) -> None:
        if not resp.ok:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text}")

    def _request(self, method: str, path: str, **kwargs):
        """Issue an HTTP request to the config service with bounded transport retries.

        Dispatches to the named ``requests.get`` / ``requests.post`` (not ``requests.request``)
        so callers and tests keep a stable, mockable surface. Retries transient failures via
        ``run_upstream`` and raises on a non-OK response (``_check``), so callers just consume
        ``resp.json()``. Retrying at this leaf boundary means composite methods
        (``create_service_role`` / ``create_service_scope``) retry each sub-request without
        compounding.
        """
        caller = getattr(requests, method.lower())

        def _do():
            resp = caller(f"{self._base_url()}{path}", **kwargs)
            self._check(resp)
            return resp

        return run_upstream(_do)

    def _build_subject(self, raw: dict, all_roles: dict[str, Role]) -> Subject:
        subject_id = raw["id"]
        assignments_resp = self._request(
            "GET", f"/subjects/{subject_id}/assignments", params=self._params()
        )
        realm_role_ids = {r["id"] for r in assignments_resp.json().get("realmMappings", [])}
        roles = [r.model_dump() for r in all_roles.values() if r.id in realm_role_ids]
        return Subject.model_validate({**raw, "roles": roles})

    def get_subjects(self) -> list[Subject]:
        resp = self._request("GET", "/subjects", params=self._params())
        all_roles = self._all_roles_map()
        return [self._build_subject(raw, all_roles) for raw in resp.json()]

    def get_roles(self) -> list[Role]:
        resp = self._request("GET", "/roles", params=self._params())
        roles = []
        for raw in resp.json():
            role_data = dict(raw)
            if raw.get("composite"):
                composites_resp = self._request(
                    "GET", f"/roles/{raw['name']}/composites", params=self._params()
                )
                role_data["childRoles"] = composites_resp.json()
            roles.append(Role.model_validate(role_data))
        return roles

    def _build_service(self, raw: dict, all_roles: dict[str, Role], all_scopes: dict[str, Scope]) -> Service:
        service_id = raw["id"]
        roles_resp = self._request(
            "GET", f"/services/{service_id}/roles", params=self._params()
        )
        scopes_resp = self._request(
            "GET", f"/services/{service_id}/scopes", params=self._params()
        )
        # The per-service roles response carries the authoritative kind/actorIds for each role
        # (e.g. kind=Agent + actorIds=[serviceId] for agent-owned roles). Merge those fields into
        # the fully-validated all_roles objects (which carry composite/attributes/etc.) so Role
        # validation succeeds and kind/actorIds are not reverted to their defaults.
        service_roles_by_id = {r["id"]: r for r in roles_resp.json()}
        roles = []
        for role_id, svc_role in service_roles_by_id.items():
            base = all_roles.get(role_id)
            if base is not None:
                merged = {**base.model_dump(), **{k: v for k, v in svc_role.items() if k in ("kind", "actorIds")}}
            else:
                merged = svc_role  # not in all_roles (e.g. client role not in realm roles)
            roles.append(merged)
        client_id = raw.get("clientId") or raw.get("serviceId") or service_id
        service_scope_ids = {s["id"] for s in scopes_resp.json()}
        scopes = [{**s.model_dump(), "serviceId": client_id} for s in all_scopes.values() if s.id in service_scope_ids]
        # Type resolution is handled entirely by Service._resolve_keycloak_fields
        # (client.type attribute → None); the library does not infer it here.
        return Service.model_validate({**raw, "roles": roles, "scopes": scopes})

    def _all_roles_map(self) -> dict[str, Role]:
        return {r.id: r for r in self.get_roles()}

    def _all_scopes_map(self) -> dict[str, Scope]:
        return {s.id: s for s in self.get_scopes()}

    def get_services(self) -> list[Service]:
        resp = self._request("GET", "/services", params=self._params())
        all_roles = self._all_roles_map()
        all_scopes = self._all_scopes_map()
        return [self._build_service(raw, all_roles, all_scopes) for raw in resp.json()]

    def get_service(self, service_id: str) -> Service:
        resp = self._request("GET", f"/services/{service_id}", params=self._params())
        return self._build_service(resp.json(), self._all_roles_map(), self._all_scopes_map())

    def mint_discovery_token(self, service_id: str) -> str:
        """Mint a bearer token whose ``aud`` contains the tool's clientId, for authenticating UC-1
        tool discovery against the tool's AuthBridge sidecar. The config service (which holds the
        Keycloak admin) does the minting; this returns the raw ``access_token`` string. Raises
        ``RuntimeError`` on a non-OK response (via ``_check``)."""
        resp = self._request(
            "GET", f"/services/{service_id}/discovery-token", params=self._params()
        )
        return resp.json()["access_token"]

    def get_services_by_role(self, role: Role) -> list[Service]:
        """Services whose service-account holds ``role`` (client-side filter of get_services)."""
        return [s for s in self.get_services() if any(r.id == role.id for r in s.roles)]

    def get_subjects_by_role(self, role: Role) -> list[Subject]:
        resp = self._request(
            "GET", "/subjects", params={"role_id": role.id, "realm": self.realm}
        )
        return [Subject.model_validate(s) for s in resp.json()]

    def get_services_by_scope(self, scope: Scope) -> list[Service]:
        """Services exposing ``scope`` as a default client scope (client-side filter of get_services)."""
        return [s for s in self.get_services() if any(sc.id == scope.id for sc in s.scopes)]

    def get_scopes(self) -> list[Scope]:
        resp = self._request("GET", "/scopes", params=self._params())
        return [Scope.model_validate(s) for s in resp.json()]

    def create_scope(self, scope_name: str, scope_description: str) -> Scope:
        resp = self._request(
            "POST",
            "/scopes",
            json={"name": scope_name, "description": scope_description},
            params=self._params(),
        )
        return Scope.model_validate(resp.json())

    def map_scope_to_service(self, service: Service, scope: Scope) -> Service:
        self._request(
            "POST", f"/services/{service.id}/scopes/{scope.id}", params=self._params()
        )
        get_resp = self._request("GET", f"/services/{service.id}", params=self._params())
        return Service.model_validate(get_resp.json())

    def set_service_type(self, service: Service, service_type: ServiceType) -> Service:
        """Persist a service's type onto the Keycloak client as the ``client.type`` attribute.

        The value is stored capitalized (``Agent``/``Tool`` — ``ServiceType``'s values) so
        ``Service._resolve_keycloak_fields`` resolves it back on read. Returns the updated
        ``Service``. A bare ``"Agent"``/``"Tool"`` string is accepted too (``ServiceType`` is a
        ``str`` enum).
        """
        value = service_type.value if isinstance(service_type, ServiceType) else service_type
        resp = self._request(
            "POST",
            f"/services/{service.id}/type",
            json={"type": value},
            params=self._params(),
        )
        return Service.model_validate(resp.json())

    def create_service_role(self, service_id: str, role: _NamedDefinition) -> Role:
        """Idempotent create-or-get of a realm role by name, then map it to ``service_id``.

        If a realm role with ``role.name`` already exists it is reused (no duplicate create);
        otherwise it is created. The role is then mapped to the service's service-account
        (``map_role_to_service`` is itself idempotent). Returns the resolved ``Role``.
        """
        existing = next((r for r in self.get_roles() if r.name == role.name), None)
        resolved = existing or self.create_role(role.name, role.description)
        self.map_role_to_service(self.get_service(service_id), resolved)
        return resolved

    def create_service_scope(self, service_id: str, scope: _NamedDefinition) -> Scope:
        """Idempotent create-or-get of a client scope by name, then map it to ``service_id``.

        If a client scope with ``scope.name`` already exists it is reused; otherwise it is
        created. The scope is then mapped to the service as a default client scope
        (``map_scope_to_service`` is itself idempotent). Returns the resolved ``Scope``.
        """
        existing = next((s for s in self.get_scopes() if s.name == scope.name), None)
        resolved = existing or self.create_scope(scope.name, scope.description)
        self.map_scope_to_service(self.get_service(service_id), resolved)
        return resolved

    def create_role(self, role_name: str, role_description: str) -> Role:
        resp = self._request(
            "POST",
            "/roles",
            json={"name": role_name, "description": role_description},
            params=self._params(),
        )
        return Role.model_validate(resp.json())

    def map_role_to_service(self, service: Service, role: Role) -> Service:
        self._request(
            "POST", f"/services/{service.id}/roles/{role.id}", params=self._params()
        )
        get_resp = self._request("GET", f"/services/{service.id}", params=self._params())
        return Service.model_validate(get_resp.json())
