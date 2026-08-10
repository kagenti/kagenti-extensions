#!/usr/bin/env python3
"""Drive dev-user's intents through the demo: ROPC login, inbound gate, a real RFC 8693 token
exchange, then the per-intent outbound gate — proving developer least-privilege end to end against
``generated/02-after-tool/``."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from _lib import drive

if __name__ == "__main__":
    drive("dev-user")
