import os
from pathlib import Path

import requests
from dotenv import load_dotenv

from aiac.idp.configuration.models import Subject, Role, Service, Scope

load_dotenv(Path(__file__).resolve().parent / ".env")


class Configuration:
    def __init__(self, realm: str) -> None:
        self.realm = realm

    @classmethod
    def for_realm(cls, realm: str) -> "Configuration":
        return cls(realm)

    def _base_url(self) -> str:
        return os.getenv("AIAC_PDP_CONFIG_URL", "http://127.0.0.1:7071")

    def _params(self) -> dict[str, str]:
        return {"realm": self.realm}

    def _check(self, resp) -> None:
        if not resp.ok:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text}")

    def _build_subject(self, raw: dict, all_roles: dict[str, Role]) -> Subject:
        subject_id = raw["id"]
        assignments_resp = requests.get(
            f"{self._base_url()}/subjects/{subject_id}/assignments", params=self._params()
        )
        self._check(assignments_resp)
        realm_role_ids = {r["id"] for r in assignments_resp.json().get("realmMappings", [])}
        roles = [r.model_dump() for r in all_roles.values() if r.id in realm_role_ids]
        return Subject.model_validate({**raw, "roles": roles})

    def get_subjects(self) -> list[Subject]:
        resp = requests.get(f"{self._base_url()}/subjects", params=self._params())
        self._check(resp)
        all_roles = self._all_roles_map()
        return [self._build_subject(raw, all_roles) for raw in resp.json()]

    def get_roles(self) -> list[Role]:
        resp = requests.get(f"{self._base_url()}/roles", params=self._params())
        self._check(resp)
        roles = []
        for raw in resp.json():
            role_data = dict(raw)
            if raw.get("composite"):
                composites_resp = requests.get(
                    f"{self._base_url()}/roles/{raw['name']}/composites", params=self._params()
                )
                self._check(composites_resp)
                role_data["childRoles"] = composites_resp.json()
            roles.append(Role.model_validate(role_data))
        return roles

    def _build_service(self, raw: dict, all_roles: dict[str, Role], all_scopes: dict[str, Scope]) -> Service:
        service_id = raw["id"]
        roles_resp = requests.get(
            f"{self._base_url()}/services/{service_id}/roles", params=self._params()
        )
        self._check(roles_resp)
        scopes_resp = requests.get(
            f"{self._base_url()}/services/{service_id}/scopes", params=self._params()
        )
        self._check(scopes_resp)
        service_role_ids = {r["id"] for r in roles_resp.json()}
        roles = [r.model_dump() for r in all_roles.values() if r.id in service_role_ids]
        service_scope_ids = {s["id"] for s in scopes_resp.json()}
        scopes = [s.model_dump() for s in all_scopes.values() if s.id in service_scope_ids]
        # TEMP: infer type from description when not set by Keycloak attributes
        desc = raw.get("description") or ""
        inferred_type: str | None = None
        if "Agent" in desc:
            inferred_type = "Agent"
        elif "Tool" in desc:
            inferred_type = "Tool"
        patch = {"type": inferred_type} if inferred_type and not raw.get("type") else {}
        return Service.model_validate({**raw, "roles": roles, "scopes": scopes, **patch})

    def _all_roles_map(self) -> dict[str, Role]:
        return {r.id: r for r in self.get_roles()}

    def _all_scopes_map(self) -> dict[str, Scope]:
        return {s.id: s for s in self.get_scopes()}

    def get_services(self) -> list[Service]:
        resp = requests.get(f"{self._base_url()}/services", params=self._params())
        self._check(resp)
        all_roles = self._all_roles_map()
        all_scopes = self._all_scopes_map()
        return [self._build_service(raw, all_roles, all_scopes) for raw in resp.json()]

    def get_service(self, service_id: str) -> Service:
        resp = requests.get(f"{self._base_url()}/services/{service_id}", params=self._params())
        self._check(resp)
        return self._build_service(resp.json(), self._all_roles_map(), self._all_scopes_map())

    def get_services_by_role(self, role: Role) -> list[Service]:
        resp = requests.get(
            f"{self._base_url()}/services",
            params={"role_id": role.id, "realm": self.realm},
        )
        self._check(resp)
        return [Service.model_validate(s) for s in resp.json()]

    def get_services_by_scope(self, scope: Scope) -> list[Service]:
        resp = requests.get(
            f"{self._base_url()}/services",
            params={"scope_id": scope.id, "realm": self.realm},
        )
        self._check(resp)
        return [Service.model_validate(s) for s in resp.json()]

    def get_scopes(self) -> list[Scope]:
        resp = requests.get(f"{self._base_url()}/scopes", params=self._params())
        self._check(resp)
        return [Scope.model_validate(s) for s in resp.json()]

    def create_scope(self, scope_name: str, scope_description: str) -> Scope:
        resp = requests.post(
            f"{self._base_url()}/scopes",
            json={"name": scope_name, "description": scope_description},
            params=self._params(),
        )
        self._check(resp)
        return Scope.model_validate(resp.json())

    def map_scope_to_service(self, service: Service, scope: Scope) -> Service:
        resp = requests.post(
            f"{self._base_url()}/services/{service.id}/scopes/{scope.id}",
            params=self._params(),
        )
        self._check(resp)
        get_resp = requests.get(f"{self._base_url()}/services/{service.id}", params=self._params())
        self._check(get_resp)
        return Service.model_validate(get_resp.json())

    def create_role(self, role_name: str, role_description: str) -> Role:
        resp = requests.post(
            f"{self._base_url()}/roles",
            json={"name": role_name, "description": role_description},
            params=self._params(),
        )
        self._check(resp)
        return Role.model_validate(resp.json())

    def map_role_to_service(self, service: Service, role: Role) -> Service:
        resp = requests.post(
            f"{self._base_url()}/services/{service.id}/roles/{role.id}",
            params=self._params(),
        )
        self._check(resp)
        get_resp = requests.get(f"{self._base_url()}/services/{service.id}", params=self._params())
        self._check(get_resp)
        return Service.model_validate(get_resp.json())
