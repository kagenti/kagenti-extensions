"""Unit tests for aiac.pdp.library.policy."""

from aiac.pdp.library.policy import Policy

REALM = "kagenti"


# ---------------------------------------------------------------------------
# Factory method
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
# Default URL fallback (verifies env var is read)
# ---------------------------------------------------------------------------


def test_default_base_url_fallback(monkeypatch):
    monkeypatch.delenv("AIAC_PDP_POLICY_URL", raising=False)
    p = Policy.for_realm(REALM)
    assert p._base_url() == "http://127.0.0.1:7072"


def test_base_url_reads_from_env(monkeypatch):
    monkeypatch.setenv("AIAC_PDP_POLICY_URL", "http://custom:9999")
    p = Policy.for_realm(REALM)
    assert p._base_url() == "http://custom:9999"
