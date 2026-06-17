import os
from pathlib import Path
from typing import Literal

import requests
from dotenv import load_dotenv

from aiac.pdp.library.configuration.models import Subject, Role, Service, Scope

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

    def get_subjects(self) -> list[Subject]:
        resp = requests.get(f"{self._base_url()}/subjects", params=self._params())
        self._check(resp)
        return [Subject.model_validate(s) for s in resp.json()]

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
            scopes_resp = requests.get(
                f"{self._base_url()}/roles/{raw['name']}/scopes", params=self._params()
            )
            self._check(scopes_resp)
            role_data["mappedScopes"] = scopes_resp.json()
            roles.append(Role.model_validate(role_data))
        return roles

    def get_services(self) -> list[Service]:
        resp = requests.get(f"{self._base_url()}/services", params=self._params())
        self._check(resp)
        services = []
        for raw in resp.json():
            service_id = raw["id"]
            roles_resp = requests.get(
                f"{self._base_url()}/services/{service_id}/roles", params=self._params()
            )
            self._check(roles_resp)
            scopes_resp = requests.get(
                f"{self._base_url()}/services/{service_id}/scopes", params=self._params()
            )
            self._check(scopes_resp)
            services.append(Service.model_validate({**raw, "roles": roles_resp.json(), "scopes": scopes_resp.json()}))
        return services

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

    def set_service_type(self, service_id: str, service_type: Literal["Agent", "Tool"]) -> Service:
        resp = requests.patch(
            f"{self._base_url()}/services/{service_id}",
            json={"type": service_type},
            params=self._params(),
        )
        self._check(resp)
        return Service.model_validate(resp.json())

    def map_role_to_service(self, service: Service, role: Role) -> Service:
        resp = requests.post(
            f"{self._base_url()}/services/{service.id}/roles/{role.id}",
            params=self._params(),
        )
        self._check(resp)
        get_resp = requests.get(f"{self._base_url()}/services/{service.id}", params=self._params())
        self._check(get_resp)
        return Service.model_validate(get_resp.json())
