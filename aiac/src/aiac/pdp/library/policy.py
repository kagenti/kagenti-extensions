import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")


class Policy:
    def __init__(self, realm: str) -> None:
        self.realm = realm

    @classmethod
    def for_realm(cls, realm: str) -> "Policy":
        return cls(realm)

    def _base_url(self) -> str:
        return os.getenv("AIAC_PDP_POLICY_URL", "http://127.0.0.1:7072")

    def add_role_composites(self, role_name: str, composites: list[dict]) -> None:
        raise NotImplementedError("add_role_composites not yet implemented")

    def remove_role_composites(self, role_name: str, composites: list[dict]) -> None:
        raise NotImplementedError("remove_role_composites not yet implemented")
