"""Unit tests for aiac.agent.shared.nodes (issue 4.1)."""

import sys
from unittest.mock import MagicMock, patch

import pytest


def _make_state(trigger_type: str = "role/r1", realm: str = "kagenti") -> dict:
    return {
        "trigger": {"trigger_type": trigger_type, "entity_id": None},
        "realm": realm,
        "policy_chunks": [],
        "domain_knowledge_chunks": [],
        "pdp_snapshot": None,
        "proposed_diff": None,
        "validation_errors": [],
        "added": [],
        "removed": [],
        "summary": "",
    }


def _make_chroma_module(docs: list[str] | None = None):
    """Build a mock chromadb module whose HttpClient returns fake query results."""
    collection = MagicMock()
    collection.query.return_value = {"documents": [docs if docs is not None else ["chunk1"]]}
    client = MagicMock()
    client.get_collection.return_value = collection
    mock_chroma = MagicMock()
    mock_chroma.HttpClient.return_value = client
    return mock_chroma


def _make_chroma_module_unavailable(exc: Exception | None = None):
    """Build a mock chromadb module that raises on every HttpClient call."""
    if exc is None:
        exc = OSError("connection refused")
    mock_chroma = MagicMock()
    mock_chroma.HttpClient.side_effect = exc
    return mock_chroma


# ---------------------------------------------------------------------------
# fetch_policy
# ---------------------------------------------------------------------------


class TestFetchPolicy:
    def test_successful_fetch_populates_policy_chunks(self):
        from aiac.agent.shared.nodes import fetch_policy

        state = _make_state()
        mock_chroma = _make_chroma_module(docs=["policy-doc-1", "policy-doc-2"])

        with patch.dict("sys.modules", {"chromadb": mock_chroma}):
            result = fetch_policy(state)

        assert result["policy_chunks"] == ["policy-doc-1", "policy-doc-2"]

    def test_build_trigger_uses_correct_query(self):
        from aiac.agent.shared.nodes import fetch_policy

        state = _make_state(trigger_type="build")
        mock_chroma = _make_chroma_module()

        with patch.dict("sys.modules", {"chromadb": mock_chroma}):
            fetch_policy(state)

        collection = mock_chroma.HttpClient.return_value.get_collection.return_value
        call_kwargs = collection.query.call_args
        assert "all access control rules" in str(call_kwargs)

    def test_rebuild_trigger_uses_correct_query(self):
        from aiac.agent.shared.nodes import fetch_policy

        state = _make_state(trigger_type="rebuild")
        mock_chroma = _make_chroma_module()

        with patch.dict("sys.modules", {"chromadb": mock_chroma}):
            fetch_policy(state)

        collection = mock_chroma.HttpClient.return_value.get_collection.return_value
        call_kwargs = collection.query.call_args
        assert "all access control rules" in str(call_kwargs)

    def test_role_trigger_uses_correct_query(self):
        from aiac.agent.shared.nodes import fetch_policy

        state = _make_state(trigger_type="role/r1")
        mock_chroma = _make_chroma_module()

        with patch.dict("sys.modules", {"chromadb": mock_chroma}):
            fetch_policy(state)

        collection = mock_chroma.HttpClient.return_value.get_collection.return_value
        call_kwargs = collection.query.call_args
        assert "role assignment rules" in str(call_kwargs)

    def test_service_trigger_uses_correct_query(self):
        from aiac.agent.shared.nodes import fetch_policy

        state = _make_state(trigger_type="service/svc-1")
        mock_chroma = _make_chroma_module()

        with patch.dict("sys.modules", {"chromadb": mock_chroma}):
            fetch_policy(state)

        collection = mock_chroma.HttpClient.return_value.get_collection.return_value
        call_kwargs = collection.query.call_args
        assert "service access control rules" in str(call_kwargs)

    def test_chroma_unavailable_raises_503(self):
        from aiac.agent.shared.nodes import fetch_policy
        from fastapi import HTTPException

        state = _make_state()
        mock_chroma = _make_chroma_module_unavailable()

        with patch.dict("sys.modules", {"chromadb": mock_chroma}):
            with pytest.raises(HTTPException) as exc_info:
                fetch_policy(state)

        assert exc_info.value.status_code == 503

    def test_chroma_n_results_respected(self, monkeypatch):
        from aiac.agent.shared.nodes import fetch_policy

        monkeypatch.setenv("CHROMA_N_RESULTS", "5")
        state = _make_state()
        mock_chroma = _make_chroma_module()

        with patch.dict("sys.modules", {"chromadb": mock_chroma}):
            fetch_policy(state)

        collection = mock_chroma.HttpClient.return_value.get_collection.return_value
        call_kwargs = collection.query.call_args
        assert "5" in str(call_kwargs) or call_kwargs.kwargs.get("n_results") == 5 or call_kwargs[1].get("n_results") == 5

    def test_queries_aiac_policies_collection(self):
        from aiac.agent.shared.nodes import fetch_policy

        state = _make_state()
        mock_chroma = _make_chroma_module()

        with patch.dict("sys.modules", {"chromadb": mock_chroma}):
            fetch_policy(state)

        client = mock_chroma.HttpClient.return_value
        client.get_collection.assert_called_with("aiac-policies")


# ---------------------------------------------------------------------------
# fetch_domain_knowledge
# ---------------------------------------------------------------------------


class TestFetchDomainKnowledge:
    def test_successful_fetch_populates_domain_knowledge_chunks(self):
        from aiac.agent.shared.nodes import fetch_domain_knowledge

        state = _make_state()
        mock_chroma = _make_chroma_module(docs=["domain-doc-1"])

        with patch.dict("sys.modules", {"chromadb": mock_chroma}):
            result = fetch_domain_knowledge(state)

        assert result["domain_knowledge_chunks"] == ["domain-doc-1"]

    def test_empty_collection_returns_empty_list_without_error(self):
        from aiac.agent.shared.nodes import fetch_domain_knowledge

        state = _make_state()
        mock_chroma = _make_chroma_module(docs=[])  # empty result set

        with patch.dict("sys.modules", {"chromadb": mock_chroma}):
            result = fetch_domain_knowledge(state)

        assert result["domain_knowledge_chunks"] == []

    def test_chroma_unavailable_raises_503(self):
        """ChromaDB being down is fatal for domain knowledge too — raises 503."""
        from aiac.agent.shared.nodes import fetch_domain_knowledge
        from fastapi import HTTPException

        state = _make_state()
        mock_chroma = _make_chroma_module_unavailable()

        with patch.dict("sys.modules", {"chromadb": mock_chroma}):
            with pytest.raises(HTTPException) as exc_info:
                fetch_domain_knowledge(state)

        assert exc_info.value.status_code == 503

    def test_queries_aiac_domain_knowledge_collection(self):
        from aiac.agent.shared.nodes import fetch_domain_knowledge

        state = _make_state()
        mock_chroma = _make_chroma_module()

        with patch.dict("sys.modules", {"chromadb": mock_chroma}):
            fetch_domain_knowledge(state)

        client = mock_chroma.HttpClient.return_value
        client.get_collection.assert_called_with("aiac-domain-knowledge")

    def test_chroma_n_results_respected(self, monkeypatch):
        from aiac.agent.shared.nodes import fetch_domain_knowledge

        monkeypatch.setenv("CHROMA_N_RESULTS", "3")
        state = _make_state()
        mock_chroma = _make_chroma_module()

        with patch.dict("sys.modules", {"chromadb": mock_chroma}):
            fetch_domain_knowledge(state)

        collection = mock_chroma.HttpClient.return_value.get_collection.return_value
        call_kwargs = collection.query.call_args
        assert "3" in str(call_kwargs) or call_kwargs.kwargs.get("n_results") == 3 or call_kwargs[1].get("n_results") == 3
