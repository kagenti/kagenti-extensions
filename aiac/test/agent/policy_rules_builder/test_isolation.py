"""Architecture guard: the PRB must not import the PDP/Policy-Model-Store libraries.

Only the PCE touches aiac.pdp.policy.library / aiac.policy.model_store.library. A
sys.modules check gives false positives (other imported modules pull those in),
so we AST-scan the PRB package's own source instead.
"""

import ast
import pathlib

import aiac.agent.policy_rules_builder as prb

FORBIDDEN = ("aiac.pdp.policy.library", "aiac.policy.model_store.library")


def test_prb_imports_no_pdp_or_store_library():
    pkg_dir = pathlib.Path(prb.__file__).parent
    seen: set[str] = set()
    for py in pkg_dir.rglob("*.py"):
        for node in ast.walk(ast.parse(py.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                seen.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                seen.add(node.module)
    leaked = [m for m in seen for f in FORBIDDEN if m == f or m.startswith(f + ".")]
    assert not leaked, f"PRB leaked forbidden imports: {leaked}"
