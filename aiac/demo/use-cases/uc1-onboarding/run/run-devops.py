#!/usr/bin/env python3
"""Drive devops-user through the demo: ROPC login, then the inbound gate — where devops-user is
denied. Stopping there is the intended story (devops-user has no realm role that sources any agent
scope), not an error; the outbound gate and the RFC 8693 exchange are never reached for this user."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from _lib import drive

if __name__ == "__main__":
    drive("devops-user")
