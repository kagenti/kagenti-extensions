# Component PRD: Policy Guardrails Agent

## Description

A FastAPI verification service co-located with ChromaDB and the RAG Ingest Service in the RAG Pod. It sits between the RAG Ingest Service and ChromaDB: before any document from an ingest request is written, the RAG Ingest Service calls the Policy Guardrails Agent to obtain a verdict on that document. It is reachable only on the RAG Pod's loopback network (`localhost:7075`) — it is not exposed on the `aiac-rag-service` ClusterIP Service, so the RAG Ingest Service is structurally the only caller.

The agent evaluates one document per call. It may query the co-located ChromaDB instance for evaluation context, including the current (pre-update) version of the same `doc_id` when one exists.

**One service, two API families.** The module is a single container / image (`aiac-policy-guardrails`) on a single port (`:7075`), reached through a single `AIAC_GUARDRAILS_URL`. It exposes two independently-developed verification API families — one for the `policy` collection (`aiac-policies`) and one for the `domain-knowledge` collection (`aiac-domain-knowledge`). The RAG Ingest Service selects the family by collection slug. **This spec defines the `policy` API only**; the `domain-knowledge` API is specced independently later and mirrors the same wiring and verdict conventions.

For the `policy` collection the agent performs two checks — **policy hygiene** and **contradiction against the existing corpus** — detailed under [Responsibilities and check set](#responsibilities-and-check-set). Some aspects of the surface remain unresolved — see [Open decisions (TBD)](#open-decisions-tbd). This spec fixes the agent's place in the architecture, its `policy`-family responsibilities and internal design, and its contract obligations with the RAG Ingest Service.

## Endpoints

**Route paths and wire-schema TBD** — see [Open decisions (TBD)](#open-decisions-tbd). The verification route path and the exact JSON field names of the request/verdict payloads are unresolved. What the request **must convey** is fixed, however (see the contradiction decision below): **operation** (`replace` | `update`), **collection**, **doc_id**, and the document **text**. A `/health` endpoint is expected, following the convention used by every other AIAC service.

## Verification contract

These behaviors are fixed regardless of what the endpoint surface ends up looking like:

- Invoked by the RAG Ingest Service on all 12 write endpoints (`replace` and `update`, each across `text`/`file`/`url`) for every collection slug in `AIAC_RAG_COLLECTIONS`. `DELETE /ingest/{collection}/{doc_id}` is exempt — removal introduces no new text to verify.
- Called once per document. A multi-document request (a multi-doc `replace` body, or a multipart `/file` upload with several files) results in one call per document.
- **Pre-flight**: every document in a request is verified before the RAG Ingest Service makes any ChromaDB mutation for that request.
- **All-or-nothing**: if any document in a request is rejected, the whole ingest request fails and nothing is written — the collection is left exactly as it was. This preserves the RAG Ingest Service's existing collection-level atomicity guarantee for `replace`.
- **Fail-closed**: if the agent is unreachable, times out, or errors, the RAG Ingest Service treats this as a failure and writes nothing. `AIAC_GUARDRAILS_ENABLED` (on the RAG Ingest Service) is the explicit operator off-switch — with it set to `false` the RAG Ingest Service skips verification entirely. This keeps "guardrails disabled" distinguishable from "guardrails enabled but broken."
- No Event Broker interaction. The agent neither publishes nor consumes NATS subjects. The RAG Ingest Service's existing `aiac.apply.policy.build` publish behavior is unchanged: a fail-closed rejection means the ingest request never succeeds, so the build event is simply never published for that request.

## Responsibilities and check set

The `policy` API runs two checks against a single incoming `aiac-policies` document. Both are LLM-backed (hygiene and contradiction over natural-language policy are inherently semantic).

### Check 1 — Policy hygiene (intrinsic; single document, no corpus)

> **Detailed design:** [policy-guardrails-agent-policy-hygiene.md](policy-guardrails-agent-policy-hygiene.md) — the finding-code taxonomy, severity derivation, pre-LLM guards, and structured-output contract for this check.

Three facets of the document's intrinsic quality:

- **On-topic & well-formed** — the document must actually express access-control intent (some subject/role → service/scope/action). Empty, gibberish, or off-topic text is rejected.
- **Actionable / translatable** — the statement must be specific enough for the downstream AIAC Agent LLM to emit Rego: it references resolvable roles/services/scopes/actions and avoids hopeless vagueness ("be secure", "do the right thing").
- **Internally consistent & clear** — the document does not contradict *itself* and is not so ambiguous it would yield nondeterministic Rego. (Self-contradiction; distinct from corpus contradiction below.)

Prompt-injection / adversarial-content screening is **out of scope for this increment** (deferred; it was always a separate concern from hygiene).

### Check 2 — Contradiction against the existing corpus (relational)

Whether the incoming document conflicts with policy already in the collection. The baseline is **the persistent corpus only** — what will still exist after the write:

- **`update`** → the incoming document is compared against the persistent ChromaDB corpus (all documents that survive the upsert), **excluding this `doc_id`'s own prior version**. Replacing a document with a reversed policy is a legitimate change, not a contradiction, so the prior version is never treated as a conflicting peer.
- **`replace`** → **no** corpus-contradiction check. `replace` drops and recreates the collection, redefining it atomically; the pre-write corpus is about to be deleted, so flagging a conflict against soon-to-be-removed documents would be wrong. `replace` documents receive hygiene (including internal self-consistency) only.
- **Intra-batch contradiction** (one document in a request conflicting with a *sibling* document in the same request) is **not** detected in this increment — a documented deferral for both operations. Because verification is one-call-per-document and pre-flight, sibling documents are not yet in ChromaDB and are not passed in the call.

This is why the verify request must convey the **operation** and **collection**: the agent cannot otherwise interpret ChromaDB contents correctly (a `replace` reading the pre-write corpus would produce false contradictions).

## Agent design

Built on **LangGraph** to match the AIAC Agent stack. A `StateGraph` runs the checks staged, with a short-circuit so a failed hygiene check never triggers an unnecessary corpus scan or contradiction LLM call.

**Graph state** (per verification call): the input document (`text`, `doc_id`), `operation`, `collection`, the fetched `corpus` (populated only for `update`), the accumulated `findings`, and the final `verdict`.

**Nodes and edges:**

```
        ┌──────────┐
 in ──► │ hygiene  │  (1 LLM call; no corpus)
        └────┬─────┘
             │ any blocking hygiene finding
             ├───────────────────────────────► verdict   (short-circuit)
             │ hygiene clean
             ▼
      operation == update ? ──── no (replace) ──► verdict
             │ yes
             ▼
        ┌──────────────┐      ┌───────────────┐
        │ corpus_fetch │ ───► │ contradiction │ ──► verdict
        └──────────────┘      └───────────────┘   (1 LLM call)
```

- **`hygiene`** — one LLM call judging the three hygiene facets; emits findings.
- Conditional edge — if hygiene produced any *blocking* finding, jump straight to `verdict` (judging contradiction on malformed text is noise and wastes a corpus scan). Otherwise, if `operation == update`, proceed to `corpus_fetch`; if `operation == replace`, jump to `verdict`.
- **`corpus_fetch`** — **full-corpus scan**: `.get()` all documents in the collection from ChromaDB (no similarity search, so the agent needs no embedding-API dependency), regroup chunks by `doc_id`, and drop this document's own prior version. A full scan catches logically-opposed-but-dissimilar contradictions ("admins may delete anything" vs "production is immutable") that top-K retrieval would miss. Guarded by `GUARDRAILS_MAX_CORPUS_DOCS` — see [Corpus-scan guard](#corpus-scan-guard).
- **`contradiction`** — one LLM call judging the incoming document against the fetched corpus; emits findings naming the conflicting `related_doc_id`.
- **`verdict`** — aggregates findings into the final verdict (see below).

### Findings and verdict model

Each check emits structured findings: `{check, severity, message, related_doc_id?}` where `severity ∈ {blocking, advisory}`.

- **Verdict = reject** iff at least one **blocking** finding is present; otherwise **accept**.
- **Advisory** findings are returned but never block — a non-fatal channel so the LLM can flag concerns without failing the operator's whole ingest request (recall all-or-nothing).
- The LLM is instructed which conditions are blocking (not-a-policy, untranslatable, self-contradiction, corpus-contradiction) vs advisory (style, redundancy, minor vagueness).

LLM structured output is obtained via the LangChain structured-output surface (e.g. `with_structured_output`) so findings are validated at the call boundary.

### Corpus-scan guard

`GUARDRAILS_MAX_CORPUS_DOCS` bounds the full-corpus scan. If the collection exceeds the limit, the `corpus_fetch` / `contradiction` path **fails-closed** (the agent returns an error, which the RAG Ingest Service treats as a rejection), consistent with the module's fail-closed posture. This keeps a large corpus from silently degrading to partial or truncated contradiction coverage.

## Configuration

| Variable | Default | Source |
|----------|---------|--------|
| `CHROMA_URL` | `http://localhost:8000` | ConfigMap |
| `LLM_BASE_URL` | — | ConfigMap |
| `LLM_MODEL` | — | ConfigMap |
| `LLM_API_KEY` | — | Kubernetes Secret |
| `GUARDRAILS_MAX_CORPUS_DOCS` | `500` | ConfigMap |

`LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY` reuse the existing AIAC LLM convention (same trio the AIAC Agent and integration tests use).

Corresponding variables on the **RAG Ingest Service** side (documented in [rag-ingest-service.md](rag-ingest-service.md)): `AIAC_GUARDRAILS_URL`, `AIAC_GUARDRAILS_ENABLED`, `AIAC_GUARDRAILS_TIMEOUT_SECONDS`.

## Runtime

- Framework: FastAPI with uvicorn
- Bind: `0.0.0.0:7075`
- Base image: `python:3.12-slim`
- Runs as non-root UID 10001 per the AIAC container pattern.

## Dependencies (`requirements.txt`)

```
fastapi
uvicorn[standard]
httpx
chromadb
langgraph
langchain-openai
```

(`langchain-openai` is the OpenAI-compatible LLM client for the LangGraph nodes; swap for the equivalent client if the chosen `LLM_BASE_URL` provider differs.)

## Open decisions (TBD)

1. **Endpoint surface and verdict wire-schema** — the verification route path(s) and the exact JSON field names of the request/verdict payloads. The *information* the request must carry (operation, collection, doc_id, text) and the verdict/findings *model* (accept/reject + two-level-severity findings) are fixed above; only the wire representation is open.
2. **Strictness calibration** — how conservative the hygiene/contradiction prompts are about emitting *blocking* (vs advisory) findings, given that one false blocking finding fails the operator's whole request.
3. **Findings persistence** — whether rejection findings are only returned synchronously in the response (plus structured logging), or also persisted somewhere for audit. Deferred this increment: synchronous response + stdout logging only.
4. **Operator override** — whether an operator can force-accept a rejected document, and through what path. Deferred this increment: the RAG Ingest Service's `AIAC_GUARDRAILS_ENABLED` global off-switch is the only override.
5. **`domain-knowledge` API family** — the second verification API (factual-coherence / contradiction for `aiac-domain-knowledge`) is specced independently later.
