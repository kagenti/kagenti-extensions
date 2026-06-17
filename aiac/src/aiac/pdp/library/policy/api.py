import os
from pathlib import Path

import requests
from dotenv import load_dotenv

from aiac.pdp.library.policy.models import PolicyModel

load_dotenv(Path(__file__).resolve().parent / ".env")


class Policy:
    def __init__(self, realm: str) -> None:
        self.realm = realm

    @classmethod
    def for_realm(cls, realm: str) -> "Policy":
        return cls(realm)

    def _base_url(self) -> str:
        return os.getenv("AIAC_PDP_POLICY_URL", "http://127.0.0.1:7072")

    def apply_policy(self, policy: PolicyModel) -> None:
        resp = requests.post(
            f"{self._base_url()}/policy",
            json=policy.model_dump(),
            params={"realm": self.realm},
        )
        if not resp.ok:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text}")
