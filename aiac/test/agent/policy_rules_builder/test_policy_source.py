"""Unit tests for the Phase 1 policy source (real FilePolicySource, real files).

These exercise FilePolicySource end-to-end via AIAC_POLICY_FILE -- get_policy_source
is NOT patched here, so the file read is real. The LLM is still mocked at
graph._structured_call.
"""

from contextlib import ExitStack
from unittest.mock import patch

import pytest

from aiac.agent.policy_rules_builder.graph import AuditVerdict, RoleSelection, build_role_rules
from aiac.idp.configuration.models import Role, Scope


def _role(id="r-edit", name="editor", composite=False, children=None) -> Role:
    return Role(id=id, name=name, composite=composite, childRoles=children or [])


def _scope(id="s-write", name="write") -> Scope:
    return Scope(id=id, name=name)


# --------------------------------------------------------------------------- #
# Slice 7 — Phase 1 reads the whole policy file at AIAC_POLICY_FILE and the      #
# proposer's user message carries that policy text (real FilePolicySource).     #
# --------------------------------------------------------------------------- #
def test_policy_file_reaches_proposer(tmp_path, monkeypatch):
    policy = tmp_path / "policy.md"
    policy.write_text("EDITORS MAY WRITE", encoding="utf-8")
    monkeypatch.setenv("AIAC_POLICY_FILE", str(policy))

    with ExitStack() as stack:
        sc = stack.enter_context(
            patch(
                "aiac.agent.policy_rules_builder.graph._structured_call",
                side_effect=[
                    RoleSelection(granted_scope_names=["write"], reasoning="r"),
                    AuditVerdict(approved=True),
                ],
            )
        )
        build_role_rules(_role(), [_scope()])

    proposer_msg = sc.call_args_list[0].args[1][1].content
    assert "EDITORS MAY WRITE" in proposer_msg


# --------------------------------------------------------------------------- #
# Slice 8 — a missing/unreadable policy file raises (OSError), never a silent   #
# []; the builder does not swallow policy-source failure.                      #
# --------------------------------------------------------------------------- #
def test_missing_policy_file_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("AIAC_POLICY_FILE", str(tmp_path / "does-not-exist.md"))

    with pytest.raises(OSError):
        build_role_rules(_role(), [_scope()])
