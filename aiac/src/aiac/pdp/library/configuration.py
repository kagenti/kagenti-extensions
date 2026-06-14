import os
from pathlib import Path

import requests
from dotenv import load_dotenv

from .models import Subject, Role, Service, Scope

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
        return [Role.model_validate(r) for r in resp.json()]

    def get_services(self) -> list[Service]:
        resp = requests.get(f"{self._base_url()}/services", params=self._params())
        self._check(resp)
        return [Service.model_validate(s) for s in resp.json()]

    def get_scopes(self) -> list[Scope]:
        resp = requests.get(f"{self._base_url()}/scopes", params=self._params())
        self._check(resp)
        return [Scope.model_validate(s) for s in resp.json()]
