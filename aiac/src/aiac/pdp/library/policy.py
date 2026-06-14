import os
from pathlib import Path

import requests
from dotenv import load_dotenv

from .models import Scope

load_dotenv(Path(__file__).resolve().parent / ".env")


class Policy:
    def __init__(self, realm: str) -> None:
        self.realm = realm

    @classmethod
    def for_realm(cls, realm: str) -> "Policy":
        return cls(realm)

    def _base_url(self) -> str:
        return os.getenv("AIAC_PDP_POLICY_URL", "http://127.0.0.1:7072")

    def _params(self) -> dict[str, str]:
        return {"realm": self.realm}

    def _check(self, resp) -> None:
        if not resp.ok:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text}")

    def create_scope(self, service_id: str, scope_name: str, description: str) -> Scope:
        url = f"{self._base_url()}/services/{service_id}/scopes"
        resp = requests.post(
            url,
            json={"name": scope_name, "description": description},
            params=self._params(),
        )
        self._check(resp)
        return Scope.model_validate(resp.json())
