# Component PRD: RAG Knowledge Base

## Description
A ChromaDB vector store that holds two named collections in a single instance: AIAC access control policies (`aiac-policies`) and org/business domain context (`aiac-domain-knowledge`). Deployed in a dedicated Kubernetes Pod alongside the RAG Ingest Service. The AIAC Agent retrieves relevant chunks from both collections at runtime via similarity search.

## Technology
ChromaDB

## Collections

| Slug (wire) | ChromaDB Collection Name | Content | Written by | Read by |
|-------------|--------------------------|---------|------------|---------|
| `policy` | `aiac-policies` | Access control policy rules in natural language | RAG Ingest Service | Agent `fetch_policy` node |
| `domain-knowledge` | `aiac-domain-knowledge` | Org/business context — team rosters, application ownership, department mappings, who-does-what | RAG Ingest Service | Agent `fetch_domain_knowledge` node |

The legal collection set is an open extension point governed by `AIAC_RAG_COLLECTIONS` on the RAG Ingest Service. Adding a new collection is a configuration-only change (new slug + ChromaDB name in the slug→name map) with no code modification required.

## Deployment
Kubernetes Deployment in the RAG Pod, co-located with the RAG Ingest Service container. Exposed via the `aiac-rag-service` ClusterIP Service on port 7080.

## Access patterns

| Consumer | Operation | Collection |
|----------|-----------|------------|
| RAG Ingest Service | Write (replace / upsert / delete) | Either collection, selected by `{collection}` slug in the request URL |
| AIAC Agent `fetch_policy` | Read (similarity search, top-N chunks) | `aiac-policies` |
| AIAC Agent `fetch_domain_knowledge` | Read (similarity search, top-N chunks) | `aiac-domain-knowledge` |

Each chunk written to ChromaDB stores `doc_id` in its metadata to enable document-level upsert and targeted deletion.
