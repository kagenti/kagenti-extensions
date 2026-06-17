"""Unit tests for aiac.pdp.library.policy."""

import pytest
from unittest.mock import MagicMock, patch

from aiac.pdp.library.policy.models import PolicyModel, PolicyStatement
from aiac.pdp.library.policy.api import Policy

REALM = "kagenti"
BASE = "http://127.0.0.1:7072"


def _ok(json_data=None, status=200):
    resp = MagicMock()
    resp.ok = True
    resp.status_code = status
    resp.json.return_value = json_data or {}
    return resp


def _err(status=500):
    resp = MagicMock()
    resp.ok = False
    resp.status_code = status
    resp.text = "internal error"
    return resp


# ---------------------------------------------------------------------------
# PolicyStatement model
# ---------------------------------------------------------------------------


class TestPolicyStatement:
    def test_has_statement_type_and_entity_refs(self):
        s = PolicyStatement(statement_type="role_mapping", entity_refs=["reader", "abc123"])
        assert s.statement_type == "role_mapping"
        assert s.entity_refs == ["reader", "abc123"]

    def test_entity_refs_accepts_empty_list(self):
        s = PolicyStatement(statement_type="role_mapping", entity_refs=[])
        assert s.entity_refs == []

    def test_model_validate(self):
        s = PolicyStatement.model_validate(
            {"statement_type": "role_mapping", "entity_refs": ["r1"]}
        )
        assert s.statement_type == "role_mapping"
        assert s.entity_refs == ["r1"]


# ---------------------------------------------------------------------------
# PolicyModel model
# ---------------------------------------------------------------------------


class TestPolicyModel:
    def test_has_statements_list(self):
        stmt = PolicyStatement(statement_type="role_mapping", entity_refs=["r1"])
        m = PolicyModel(statements=[stmt])
        assert len(m.statements) == 1
        assert m.statements[0].statement_type == "role_mapping"

    def test_statements_default_empty(self):
        m = PolicyModel(statements=[])
        assert m.statements == []

    def test_no_realm_field(self):
        m = PolicyModel(statements=[])
        assert not hasattr(m, "realm")

    def test_no_reasoning_field(self):
        m = PolicyModel(statements=[])
        assert not hasattr(m, "reasoning")

    def test_model_dump_serializable(self):
        stmt = PolicyStatement(statement_type="role_mapping", entity_refs=["r1", "svc2"])
        m = PolicyModel(statements=[stmt])
        d = m.model_dump()
        assert d == {"statements": [{"statement_type": "role_mapping", "entity_refs": ["r1", "svc2"]}]}


# ---------------------------------------------------------------------------
# Policy factory method
# ---------------------------------------------------------------------------


class TestForRealm:
    def test_returns_policy_bound_to_realm(self):
        p = Policy.for_realm(REALM)
        assert isinstance(p, Policy)
        assert p.realm == REALM

    def test_direct_init_sets_realm(self):
        p = Policy(REALM)
        assert p.realm == REALM


# ---------------------------------------------------------------------------
# Default URL fallback
# ---------------------------------------------------------------------------


def test_default_base_url_fallback(monkeypatch):
    monkeypatch.delenv("AIAC_PDP_POLICY_URL", raising=False)
    p = Policy.for_realm(REALM)
    assert p._base_url() == "http://127.0.0.1:7072"


def test_base_url_reads_from_env(monkeypatch):
    monkeypatch.setenv("AIAC_PDP_POLICY_URL", "http://custom:9999")
    p = Policy.for_realm(REALM)
    assert p._base_url() == "http://custom:9999"


# ---------------------------------------------------------------------------
# Policy.apply_policy
# ---------------------------------------------------------------------------


class TestApplyPolicy:
    def _make_model(self) -> PolicyModel:
        return PolicyModel(
            statements=[PolicyStatement(statement_type="role_mapping", entity_refs=["reader", "svc1"])]
        )

    def test_posts_to_correct_url(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_POLICY_URL", BASE)
        model = self._make_model()
        with patch("aiac.pdp.library.policy.api.requests.post", return_value=_ok()) as m:
            Policy.for_realm(REALM).apply_policy(model)
        url = m.call_args[0][0]
        assert url == f"{BASE}/policy"

    def test_forwards_realm_as_query_param(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_POLICY_URL", BASE)
        model = self._make_model()
        with patch("aiac.pdp.library.policy.api.requests.post", return_value=_ok()) as m:
            Policy.for_realm(REALM).apply_policy(model)
        params = m.call_args[1].get("params", {})
        assert params == {"realm": REALM}

    def test_serializes_policy_model_as_json(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_POLICY_URL", BASE)
        model = self._make_model()
        with patch("aiac.pdp.library.policy.api.requests.post", return_value=_ok()) as m:
            Policy.for_realm(REALM).apply_policy(model)
        body = m.call_args[1].get("json", {})
        assert body == model.model_dump()

    def test_raises_on_non_2xx(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_POLICY_URL", BASE)
        model = self._make_model()
        with patch("aiac.pdp.library.policy.api.requests.post", return_value=_err()):
            with pytest.raises(RuntimeError):
                Policy.for_realm(REALM).apply_policy(model)

    def test_returns_none(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_POLICY_URL", BASE)
        model = self._make_model()
        with patch("aiac.pdp.library.policy.api.requests.post", return_value=_ok()):
            result = Policy.for_realm(REALM).apply_policy(model)
        assert result is None
