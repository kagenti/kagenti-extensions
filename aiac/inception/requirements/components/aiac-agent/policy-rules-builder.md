# Sub-PRD: AIAC Agent — Policy Rules Builder

## Description

The **Policy Rules Builder** (PRB) is a shared module at `agent/policy_rules_builder/`. It exposes two module-level functions that the Controller calls based on the trigger type. Each function internally runs a LangGraph `StateGraph`; the Controller is decoupled from LangGraph mechanics. The PRB fetches its own RAG context from ChromaDB (both collections) and emits `list[PolicyRule]` scoped to the input. It does **not** call `aiac.pdp.policy.library` or `aiac.policy.store.library` directly; only the PCE does.

---

## Entry points

```python
def build_role_rules(role: Role, scopes: list[Scope]) -> list[PolicyRule]: ...
def build_scope_rules(roles: list[Role], scope: Scope) -> list[PolicyRule]: ...
```

**`build_role_rules`** — role-centric: "given this role, which scopes does it get?"
Used for UC3 (Role Update). Called once per role with the full set of scopes relevant to the trigger.

**`build_scope_rules`** — scope-centric: "given this scope, which roles may access it?"
Used as one of the calls for UC1 (Service Onboarding). See the Controller sub-PRD for the full UC1 dispatch pattern.

---

## Contract

| Aspect | Decision |
|---|---|
| Structure | LangGraph `StateGraph` (internal graph design TBD) |
| Context retrieval | PRB fetches its own ChromaDB context from `aiac-policies` and `aiac-domain-knowledge` collections |
| Realm parameter | None — ChromaDB is not realm-scoped; inputs are pre-resolved typed objects |
| Trigger type in state | None — the function name encodes the trigger direction |
| Dedup | PRB generates a full rule set; the PCE's additive merge handles dedup on write |
| LLM call pattern | TBD (single call vs. propose → validate) |
| Error contract | Raises on LLM failure or ChromaDB failure — no silent empty-list returns |

---

## Use-case dispatch

| Use Case | Function(s) called |
|---|---|
| UC1 — Service Onboarding | Both `build_role_rules` and `build_scope_rules` (details in Controller sub-PRD) |
| UC2 — Policy Update (Build/Rebuild) | TBD |
| UC3 — Role Update | `build_role_rules(role, scopes)` |

---

## Configuration

The PRB inherits the following env vars (no additional config):

| Variable | Used for |
|---|---|
| `AIAC_CHROMADB_URL` | ChromaDB endpoint |
| `CHROMA_N_RESULTS` | Number of results per ChromaDB query (default `10`) |
| `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY` | LLM calls |
| `UPSTREAM_MAX_RETRIES` | Retry budget for LLM and ChromaDB calls (tenacity) |
