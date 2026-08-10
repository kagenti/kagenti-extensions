import sys
from pathlib import Path

# server.py importable as `server`
sys.path.insert(0, str(Path(__file__).parents[1]))

# scenario.py importable as `scenario`
sys.path.insert(0, str(Path(__file__).parents[5] / "test" / "integration"))
