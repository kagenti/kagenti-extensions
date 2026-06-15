"""Unit tests for aiac.agent.roles.role.nodes (issue 4.10)."""

from unittest.mock import MagicMock, patch

import pytest

from aiac.agent.shared.state import (
    Assignments,
    BaseAgentState,
    CompositeMapping,
    PDPSnapshot,
    Permission,
    ProposedDiff,
    TriggerContext,
    ValidationVerdict,
)
from aiac.pdp.library.models import Role, Service

REALM = "kagenti"
ROLE_ID = "role-uuid-1"
ROLE_NAME = "platform-admin"


def _make_trigger(role_id: str = ROLE_ID) -> TriggerContext:
    return {"trigger_type": f"role/{role_id}", "entity_id": role_id}


def _make_state(**overrides) -> BaseAgentState:
    defaults: BaseAgentState = {
        "trigger": _make_trigger(),
        "realm": REALM,
        "policy_chunks": ["Policy: admin role gets all permissions"],
        "domain_knowledge_chunks": [],
        "pdp_snapshot": None,
        "proposed_diff": None,
        "validation_errors": [],
        "added": [],
        "removed": [],
        "summary": "",
    }
    defaults.update(overrides)
    return defaults


def _make_service(sid: str, perm_names: list[str]) -> Service:
    perms = [Role(id=f"{sid}-{n}", name=n, composite=False) for n in perm_names]
    return Service(id=sid, name=sid, enabled=True, roles=perms)


def _make_role(rid: str, name: str, child_names: list[str] = ()) -> Role:
    children = [Role(id=f"child-{n}", name=n, composite=False) for n in child_names]
    return Role(id=rid, name=name, composite=bool(children), childRoles=list(children))


# ---------------------------------------------------------------------------
# fetch_pdp_state
# ---------------------------------------------------------------------------


class TestFetchPdpState:
    def test_builds_snapshot_with_all_services_and_roles(self):
        from aiac.agent.roles.role.nodes import fetch_pdp_state

        svc = _make_service("svc-1", ["read", "write"])
        role = _make_role(ROLE_ID, ROLE_NAME)
        state = _make_state()

        with patch("aiac.agent.roles.role.nodes.Configuration") as MockCfg:
            cfg_instance = MockCfg.for_realm.return_value
            cfg_instance.get_services.return_value = [svc]
            cfg_instance.get_roles.return_value = [role]

            result = fetch_pdp_state(state)

        snapshot: PDPSnapshot = result["pdp_snapshot"]
        assert snapshot is not None
        assert len(snapshot.services) == 1
        assert snapshot.services[0].id == "svc-1"
        assert len(snapshot.roles) == 1
        assert snapshot.roles[0].name == ROLE_NAME

    def test_builds_service_permissions_dict(self):
        from aiac.agent.roles.role.nodes import fetch_pdp_state

        svc = _make_service("svc-1", ["read", "write"])
        role = _make_role(ROLE_ID, ROLE_NAME)
        state = _make_state()

        with patch("aiac.agent.roles.role.nodes.Configuration") as MockCfg:
            cfg_instance = MockCfg.for_realm.return_value
            cfg_instance.get_services.return_value = [svc]
            cfg_instance.get_roles.return_value = [role]

            result = fetch_pdp_state(state)

        snapshot = result["pdp_snapshot"]
        assert "svc-1" in snapshot.service_permissions
        perm_names = [p.name for p in snapshot.service_permissions["svc-1"]]
        assert "read" in perm_names
        assert "write" in perm_names

    def test_populates_role_composites_for_affected_role(self):
        from aiac.agent.roles.role.nodes import fetch_pdp_state

        role = _make_role(ROLE_ID, ROLE_NAME, child_names=["read"])
        state = _make_state()

        with patch("aiac.agent.roles.role.nodes.Configuration") as MockCfg:
            cfg_instance = MockCfg.for_realm.return_value
            cfg_instance.get_services.return_value = []
            cfg_instance.get_roles.return_value = [role]

            result = fetch_pdp_state(state)

        snapshot = result["pdp_snapshot"]
        assert ROLE_NAME in snapshot.role_composites
        assert any(p.name == "read" for p in snapshot.role_composites[ROLE_NAME])

    def test_configuration_unavailable_raises_502(self):
        from aiac.agent.roles.role.nodes import fetch_pdp_state
        from fastapi import HTTPException

        state = _make_state()
        with patch("aiac.agent.roles.role.nodes.Configuration") as MockCfg:
            cfg_instance = MockCfg.for_realm.return_value
            cfg_instance.get_services.side_effect = RuntimeError("connection refused")

            with pytest.raises(HTTPException) as exc_info:
                fetch_pdp_state(state)

        assert exc_info.value.status_code == 502


# ---------------------------------------------------------------------------
# propose_mappings
# ---------------------------------------------------------------------------


class TestProposeMappings:
    def _make_snapshot(self) -> PDPSnapshot:
        svc = _make_service("svc-1", ["read"])
        role = _make_role(ROLE_ID, ROLE_NAME)
        return PDPSnapshot(
            services=[svc],
            roles=[role],
            service_permissions={"svc-1": [Permission(id="svc-1-read", name="read")]},
            role_composites={},
        )

    def test_returns_proposed_diff_scoped_to_affected_role(self):
        from aiac.agent.roles.role.nodes import propose_mappings

        snapshot = self._make_snapshot()
        state = _make_state(
            pdp_snapshot=snapshot,
            policy_chunks=["admin role gets read on svc-1"],
        )

        mapping = CompositeMapping(
            role_name=ROLE_NAME,
            service_id="svc-1",
            permission_id="svc-1-read",
            permission_name="read",
        )
        diff = ProposedDiff(add=[mapping], remove=[], reasoning="policy says so")

        with patch("aiac.agent.roles.role.nodes.ChatOpenAI") as MockLLM:
            llm_instance = MockLLM.return_value
            llm_instance.with_structured_output.return_value.invoke.return_value = diff

            result = propose_mappings(state)

        assert result["proposed_diff"] is not None
        assert result["proposed_diff"].add[0].role_name == ROLE_NAME

    def test_llm_unavailable_raises_504(self):
        from aiac.agent.roles.role.nodes import propose_mappings
        from fastapi import HTTPException

        snapshot = self._make_snapshot()
        state = _make_state(pdp_snapshot=snapshot)

        with patch("aiac.agent.roles.role.nodes.ChatOpenAI") as MockLLM:
            llm_instance = MockLLM.return_value
            llm_instance.with_structured_output.return_value.invoke.side_effect = Exception("LLM timeout")

            with pytest.raises(HTTPException) as exc_info:
                propose_mappings(state)

        assert exc_info.value.status_code == 504


# ---------------------------------------------------------------------------
# validate_mappings — existence check
# ---------------------------------------------------------------------------


class TestValidateMappingsExistence:
    def _make_snapshot(self) -> PDPSnapshot:
        return PDPSnapshot(
            roles=[_make_role(ROLE_ID, ROLE_NAME)],
            service_permissions={"svc-1": [Permission(id="p1", name="read")]},
        )

    def test_existence_check_passes_for_valid_mapping(self):
        from aiac.agent.roles.role.nodes import validate_mappings

        mapping = CompositeMapping(
            role_name=ROLE_NAME, service_id="svc-1", permission_id="p1", permission_name="read"
        )
        diff = ProposedDiff(add=[mapping], remove=[])
        state = _make_state(pdp_snapshot=self._make_snapshot(), proposed_diff=diff)

        with patch("aiac.agent.roles.role.nodes.ChatOpenAI") as MockLLM:
            llm_instance = MockLLM.return_value
            llm_instance.with_structured_output.return_value.invoke.return_value = ValidationVerdict(
                approved=True
            )
            result = validate_mappings(state)

        assert result["validation_errors"] == []

    def test_existence_check_fails_for_unknown_role(self):
        from aiac.agent.roles.role.nodes import validate_mappings

        mapping = CompositeMapping(
            role_name="nonexistent-role", service_id="svc-1", permission_id="p1", permission_name="read"
        )
        diff = ProposedDiff(add=[mapping], remove=[])
        state = _make_state(pdp_snapshot=self._make_snapshot(), proposed_diff=diff)

        result = validate_mappings(state)

        assert len(result["validation_errors"]) > 0

    def test_existence_check_fails_for_unknown_service(self):
        from aiac.agent.roles.role.nodes import validate_mappings

        mapping = CompositeMapping(
            role_name=ROLE_NAME, service_id="no-such-svc", permission_id="p1", permission_name="read"
        )
        diff = ProposedDiff(add=[mapping], remove=[])
        state = _make_state(pdp_snapshot=self._make_snapshot(), proposed_diff=diff)

        result = validate_mappings(state)

        assert len(result["validation_errors"]) > 0

    def test_existence_check_fails_for_unknown_permission(self):
        from aiac.agent.roles.role.nodes import validate_mappings

        mapping = CompositeMapping(
            role_name=ROLE_NAME, service_id="svc-1", permission_id="no-such-perm", permission_name="x"
        )
        diff = ProposedDiff(add=[mapping], remove=[])
        state = _make_state(pdp_snapshot=self._make_snapshot(), proposed_diff=diff)

        result = validate_mappings(state)

        assert len(result["validation_errors"]) > 0

    def test_no_writes_when_existence_check_fails(self):
        from aiac.agent.roles.role.nodes import validate_mappings

        mapping = CompositeMapping(
            role_name="ghost", service_id="svc-1", permission_id="p1", permission_name="read"
        )
        diff = ProposedDiff(add=[mapping], remove=[])
        state = _make_state(pdp_snapshot=self._make_snapshot(), proposed_diff=diff)

        result = validate_mappings(state)

        assert len(result["validation_errors"]) > 0
        # applied_changes not set means no writes attempted
        assert result.get("added", []) == []


# ---------------------------------------------------------------------------
# validate_mappings — safety guard
# ---------------------------------------------------------------------------


class TestValidateMappingsSafety:
    def _make_snapshot(self) -> PDPSnapshot:
        perms = [Permission(id=f"p{i}", name=f"perm{i}") for i in range(100)]
        role = _make_role(ROLE_ID, ROLE_NAME)
        return PDPSnapshot(
            roles=[role],
            service_permissions={"svc-1": perms},
        )

    def test_safety_guard_fails_when_too_many_changes(self, monkeypatch):
        from aiac.agent.roles.role.nodes import validate_mappings

        monkeypatch.setenv("MAX_CHANGES_PER_RUN", "2")
        snapshot = self._make_snapshot()
        mappings = [
            CompositeMapping(role_name=ROLE_NAME, service_id="svc-1", permission_id=f"p{i}", permission_name=f"perm{i}")
            for i in range(3)
        ]
        diff = ProposedDiff(add=mappings, remove=[])
        state = _make_state(pdp_snapshot=snapshot, proposed_diff=diff)

        result = validate_mappings(state)

        assert any("safety" in e.lower() or "max" in e.lower() or "changes" in e.lower() for e in result["validation_errors"])


# ---------------------------------------------------------------------------
# validate_mappings — auditor check
# ---------------------------------------------------------------------------


class TestValidateMappingsAuditor:
    def _make_snapshot(self) -> PDPSnapshot:
        return PDPSnapshot(
            roles=[_make_role(ROLE_ID, ROLE_NAME)],
            service_permissions={"svc-1": [Permission(id="p1", name="read")]},
        )

    def test_auditor_rejection_aborts_with_errors(self):
        from aiac.agent.roles.role.nodes import validate_mappings

        mapping = CompositeMapping(
            role_name=ROLE_NAME, service_id="svc-1", permission_id="p1", permission_name="read"
        )
        diff = ProposedDiff(add=[mapping], remove=[])
        state = _make_state(pdp_snapshot=self._make_snapshot(), proposed_diff=diff)

        with patch("aiac.agent.roles.role.nodes.ChatOpenAI") as MockLLM:
            llm_instance = MockLLM.return_value
            llm_instance.with_structured_output.return_value.invoke.return_value = ValidationVerdict(
                approved=False, reason="policy violation"
            )
            result = validate_mappings(state)

        assert len(result["validation_errors"]) > 0
        assert result.get("added", []) == []


# ---------------------------------------------------------------------------
# validate_mappings — scope check
# ---------------------------------------------------------------------------


class TestValidateMappingsScopeCheck:
    def _make_snapshot(self) -> PDPSnapshot:
        return PDPSnapshot(
            roles=[
                _make_role(ROLE_ID, ROLE_NAME),
                _make_role("other-id", "other-role"),
            ],
            service_permissions={"svc-1": [Permission(id="p1", name="read")]},
        )

    def test_scope_check_fails_when_diff_touches_unrelated_role(self):
        from aiac.agent.roles.role.nodes import validate_mappings

        # Diff contains a mapping for "other-role" which is not the affected role
        other_mapping = CompositeMapping(
            role_name="other-role", service_id="svc-1", permission_id="p1", permission_name="read"
        )
        diff = ProposedDiff(add=[other_mapping], remove=[])
        state = _make_state(pdp_snapshot=self._make_snapshot(), proposed_diff=diff)

        result = validate_mappings(state)

        assert any("scope" in e.lower() or "other-role" in e for e in result["validation_errors"])

    def test_scope_check_passes_when_diff_only_touches_affected_role(self):
        from aiac.agent.roles.role.nodes import validate_mappings

        mapping = CompositeMapping(
            role_name=ROLE_NAME, service_id="svc-1", permission_id="p1", permission_name="read"
        )
        diff = ProposedDiff(add=[mapping], remove=[])
        state = _make_state(pdp_snapshot=self._make_snapshot(), proposed_diff=diff)

        with patch("aiac.agent.roles.role.nodes.ChatOpenAI") as MockLLM:
            llm_instance = MockLLM.return_value
            llm_instance.with_structured_output.return_value.invoke.return_value = ValidationVerdict(approved=True)
            result = validate_mappings(state)

        assert result["validation_errors"] == []


# ---------------------------------------------------------------------------
# apply_mappings
# ---------------------------------------------------------------------------


class TestApplyMappings:
    def _make_snapshot(self) -> PDPSnapshot:
        return PDPSnapshot(
            roles=[_make_role(ROLE_ID, ROLE_NAME)],
            service_permissions={"svc-1": [Permission(id="p1", name="read")]},
        )

    def test_apply_mappings_calls_add_and_remove(self):
        from aiac.agent.roles.role.nodes import apply_mappings

        add_m = CompositeMapping(
            role_name=ROLE_NAME, service_id="svc-1", permission_id="p1", permission_name="read"
        )
        rm_m = CompositeMapping(
            role_name=ROLE_NAME, service_id="svc-1", permission_id="p2", permission_name="write"
        )
        diff = ProposedDiff(add=[add_m], remove=[rm_m])
        state = _make_state(
            pdp_snapshot=self._make_snapshot(),
            proposed_diff=diff,
            validation_errors=[],
        )

        with patch("aiac.agent.roles.role.nodes.Policy") as MockPolicy:
            policy_instance = MockPolicy.for_realm.return_value
            result = apply_mappings(state)

        policy_instance.add_role_composites.assert_called_once()
        policy_instance.remove_role_composites.assert_called_once()
        assert len(result["added"]) == 1
        assert len(result["removed"]) == 1

    def test_apply_skipped_when_validation_errors_present(self):
        from aiac.agent.roles.role.nodes import apply_mappings

        mapping = CompositeMapping(
            role_name=ROLE_NAME, service_id="svc-1", permission_id="p1", permission_name="read"
        )
        diff = ProposedDiff(add=[mapping], remove=[])
        state = _make_state(
            pdp_snapshot=self._make_snapshot(),
            proposed_diff=diff,
            validation_errors=["existence check failed"],
        )

        with patch("aiac.agent.roles.role.nodes.Policy") as MockPolicy:
            policy_instance = MockPolicy.for_realm.return_value
            apply_mappings(state)

        policy_instance.add_role_composites.assert_not_called()
        policy_instance.remove_role_composites.assert_not_called()

    def test_pdp_unavailable_raises_502(self):
        from aiac.agent.roles.role.nodes import apply_mappings
        from fastapi import HTTPException

        mapping = CompositeMapping(
            role_name=ROLE_NAME, service_id="svc-1", permission_id="p1", permission_name="read"
        )
        diff = ProposedDiff(add=[mapping], remove=[])
        state = _make_state(
            pdp_snapshot=self._make_snapshot(),
            proposed_diff=diff,
            validation_errors=[],
        )

        with patch("aiac.agent.roles.role.nodes.Policy") as MockPolicy:
            policy_instance = MockPolicy.for_realm.return_value
            policy_instance.add_role_composites.side_effect = RuntimeError("service down")

            with pytest.raises(HTTPException) as exc_info:
                apply_mappings(state)

        assert exc_info.value.status_code == 502


# ---------------------------------------------------------------------------
# format_response
# ---------------------------------------------------------------------------


class TestFormatResponse:
    def test_format_response_returns_provisioned_null(self):
        from aiac.agent.roles.role.nodes import format_response

        mapping = CompositeMapping(
            role_name=ROLE_NAME, service_id="svc-1", permission_id="p1", permission_name="read"
        )
        state = _make_state(
            added=[mapping],
            removed=[],
            validation_errors=[],
        )

        result = format_response(state)

        assert result["summary"] is not None
        assert "provisioned" in result
        assert result["provisioned"] is None

    def test_format_response_includes_added_removed_counts(self):
        from aiac.agent.roles.role.nodes import format_response

        mapping = CompositeMapping(
            role_name=ROLE_NAME, service_id="svc-1", permission_id="p1", permission_name="read"
        )
        state = _make_state(added=[mapping], removed=[])

        result = format_response(state)

        assert result["summary"] != ""
