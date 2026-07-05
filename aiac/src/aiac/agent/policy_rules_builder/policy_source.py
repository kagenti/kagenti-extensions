"""Policy-text sources for the Policy Rules Builder.

Phase 1 reads the whole policy file from ``AIAC_POLICY_FILE``. The ``PolicySource``
protocol is the seam Phase 2 (ChromaDB RAG) plugs a ``ChromaPolicySource`` into,
without changing callers.
"""

import os
from pathlib import Path
from typing import Protocol

_DEFAULT_POLICY_FILE = "/etc/aiac/policy.md"


class PolicySource(Protocol):
    def fetch(self) -> str: ...


class FilePolicySource:
    def fetch(self) -> str:
        path = Path(os.getenv("AIAC_POLICY_FILE", _DEFAULT_POLICY_FILE))
        # Missing / unreadable -> OSError propagates (no retry, no silent []).
        return path.read_text(encoding="utf-8")


def get_policy_source() -> PolicySource:
    return FilePolicySource()
