"""Shared LangGraph nodes used by all policy-applying sub-agents."""

import os

from fastapi import HTTPException

from aiac.agent.shared.state import BaseAgentState

_CHROMA_N_RESULTS_DEFAULT = 10
_UPSTREAM_MAX_RETRIES_DEFAULT = 3

_QUERY_BY_TRIGGER: dict[str, str] = {
    "build": "all access control rules",
    "rebuild": "all access control rules",
    "role": "role assignment rules",
    "service": "service access control rules",
}


def _trigger_key(trigger_type: str) -> str:
    prefix = trigger_type.split("/")[0]
    return prefix


def fetch_policy(state: BaseAgentState) -> dict:
    """Query the aiac-policies ChromaDB collection and store chunks in state."""
    import chromadb

    trigger_type = state["trigger"]["trigger_type"]
    query = _QUERY_BY_TRIGGER.get(_trigger_key(trigger_type), "all access control rules")
    n_results = int(os.getenv("CHROMA_N_RESULTS", str(_CHROMA_N_RESULTS_DEFAULT)))
    max_retries = int(os.getenv("UPSTREAM_MAX_RETRIES", str(_UPSTREAM_MAX_RETRIES_DEFAULT)))
    chroma_url = os.getenv("AIAC_CHROMADB_URL", "http://aiac-rag-service:8000")

    last_exc = None
    for _ in range(max_retries):
        try:
            client = chromadb.HttpClient(host=chroma_url.rstrip("/"))
            collection = client.get_collection("aiac-policies")
            results = collection.query(query_texts=[query], n_results=n_results)
            docs = results.get("documents", [[]])[0]
            return {**state, "policy_chunks": docs}
        except Exception as exc:
            last_exc = exc

    raise HTTPException(status_code=503, detail=f"ChromaDB unavailable: {last_exc}")


def fetch_domain_knowledge(state: BaseAgentState) -> dict:
    """Query the aiac-domain-knowledge ChromaDB collection. Non-fatal when collection is empty."""
    import chromadb

    trigger_type = state["trigger"]["trigger_type"]
    query = _QUERY_BY_TRIGGER.get(_trigger_key(trigger_type), "domain knowledge")
    n_results = int(os.getenv("CHROMA_N_RESULTS", str(_CHROMA_N_RESULTS_DEFAULT)))
    max_retries = int(os.getenv("UPSTREAM_MAX_RETRIES", str(_UPSTREAM_MAX_RETRIES_DEFAULT)))
    chroma_url = os.getenv("AIAC_CHROMADB_URL", "http://aiac-rag-service:8000")

    last_exc = None
    for _ in range(max_retries):
        try:
            client = chromadb.HttpClient(host=chroma_url.rstrip("/"))
            collection = client.get_collection("aiac-domain-knowledge")
            results = collection.query(query_texts=[query], n_results=n_results)
            docs = results.get("documents", [[]])[0]
            return {**state, "domain_knowledge_chunks": docs}
        except Exception as exc:
            last_exc = exc

    raise HTTPException(status_code=503, detail=f"ChromaDB unavailable: {last_exc}")
