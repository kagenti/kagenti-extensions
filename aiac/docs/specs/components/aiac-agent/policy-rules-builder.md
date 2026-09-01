# Sub-PRD: AIAC Agent — Policy Rules Builder

## Description

The **Policy Rules Builder** (PRB) is a shared module at `agent/policy_rules_builder/`. It
exposes two module-level functions that producing sub-agents call directly. Each function
internally runs a LangGraph `StateGraph`; callers are decoupled from LangGraph mechanics. The
PRB fetches its own policy context (see **Policy source** below), reasons over it with an LLM,
and emits `list[PolicyRule]` scoped to the input — both grants (`ALLOW`) and explicit prohibitions
(`DENY`). It does **not** call
`aiac.pdp.policy.library` or `aiac.policy.model_store.library` directly; only the PCE does.

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

Each call handles exactly **one focal entity** (the singular argument) against a list of
candidate counterparts; the caller (UC handler) does all iteration.

---

## Policy source (two phases)

Policy context is fetched behind a `PolicySource` seam, so the retrieval mechanism can change
without touching the rest of the graph.

- **Phase 1 (current):** the entire access-control policy lives in a **single file**; the PRB
  reads the whole file into the proposer prompt. No ChromaDB, no domain-knowledge collection.
  Located via `AIAC_POLICY_FILE` (default `/etc/aiac/policy.md`), read as UTF-8; a
  missing/unreadable file raises.
- **Phase 2 (later issue):** policy **and** domain knowledge live in a ChromaDB vector store;
  the PRB does RAG retrieval over the `aiac-policies` and `aiac-domain-knowledge` collections
  (query text derived from the focal entity), respecting `CHROMA_N_RESULTS`. This swaps in a
  `ChromaPolicySource` at the same seam.

---

## Contract

| Aspect | Decision |
|---|---|
| Structure | LangGraph `StateGraph` — nodes `fetch → propose → precheck → audit → build`; `audit → propose` retry edge plus an `audit → RAISE` contradiction exit; two typed graphs (role / scope) sharing node helpers |
| Context retrieval | Two-phase via a `PolicySource` seam — Phase 1 whole-file read; Phase 2 ChromaDB RAG (both collections). See **Policy source** |
| Realm parameter | None — inputs are pre-resolved typed objects; the policy source is not realm-scoped |
| Trigger type in state | None — the function name encodes the direction; no routing field in state |
| Output shape | Proposer emits **names** — granted **and** denied — plus an **exclusivity flag** (via `with_structured_output`); the PRB rebuilds `PolicyRule`s from the **typed inputs** filtered by name, never from LLM-produced fields. Result is a single mixed `list[PolicyRule]` (`effect` `ALLOW`/`DENY`), **allows-then-denies in candidate order**. DENYs = explicit `denied_names` **∪** the **derived** exclusivity complement (`candidate_set − granted` when the flag is set) |
| Dedup | PRB generates a full rule set; the PCE's additive merge handles dedup on write |
| LLM call pattern | **Propose → LLM auditor** (2 structured calls). The auditor is **three-way**: approve → build; reject → feed its reason back into propose (bounded fix-and-retry, `MAX_AUDIT_RETRIES = 3`); **genuine grant/deny contradiction → raise**. Raises on retry exhaustion |
| Empty result | An auditor-**approved** empty selection is a valid `[]` (deny-by-default). An **all-deny** result (`granted=[]`, `denied≠[]`) is a **first-class valid output** — a durable prohibition is meaningful with no current grant. Empty proposals are still audited |
| Error contract | Raises on policy-source failure, LLM failure, audit-budget exhaustion, or a genuine grant/deny **contradiction** (`PolicyContradictionError`, **fail-closed** — the focal entity's whole rule set is withheld) — no silent empty-list returns |

---

## Internal graph design

Both entry points compile the same node shape (two typed graphs sharing pure node helpers):

```
fetch ─► propose ─► precheck ─► audit ─┬─ approved ────────────► build ─► END
          ▲                            │
          └───────── retry ────────────┤   (audit feeds its reason back to propose)
                                        │
              rejected & budget exhausted ─────► RAISE  (PolicyRulesBuilderError)
                                        │
              genuine grant/deny contradiction ─► RAISE  (PolicyContradictionError)
```

- **fetch** — `PolicySource.fetch()` → `policy_text` (Phase 1: whole file).
- **propose** — proposer messages (policy + focal + candidates + any `audit_feedback`);
  `with_structured_output(Selection)` → granted names, **denied names**, an **exclusivity flag**,
  and reasoning.
- **precheck** — deterministic: filter **both** the granted and denied name lists to the candidate
  set (drop hallucinated names; log drops — symmetric on both lists). Compute and store
  `conflict_names = granted_names ∩ denied_names`. The derived exclusivity complement is disjoint
  from grants by construction, so an overlap can only arise from an explicit `denied_names` entry
  that also appears in `granted_names` (direct conflict or coarse-scope mismatch) — the genuine
  contradiction signal. No LLM.
- **audit** — auditor messages (both name sets + `conflict_names`);
  `with_structured_output(AuditVerdict)` → `{approved, reason, contradictions}`. **Three-way route:**
  `contradictions` non-empty → `raise PolicyContradictionError(focal, contradictions)`; else
  approved → build; else feed the reason back and retry, or raise `PolicyRulesBuilderError` once
  `MAX_AUDIT_RETRIES` is exhausted. When `conflict_names` is present the auditor adjudicates each
  name: a **genuine** both-grant-and-prohibit lands in `contradictions`; a proposer **generation
  error** is an ordinary rejection (reason fed back, re-propose on the shared budget). Empty
  proposals are audited too.
- **build** — reconstruct `PolicyRule`s from the typed inputs: `ALLOW` from the granted names, `DENY`
  from `denied_names ∪ (candidate_set − granted_names if exclusive else ∅)`. Return the single mixed
  list, allows-then-denies in candidate order.

### Structured-output schemas

```python
class RoleSelection(BaseModel):     # build_role_rules (role focal, scope candidates)
    granted_scope_names: list[str]
    denied_scope_names: list[str]     # explicit prohibitions
    grant_is_exclusive: bool          # focal role's access is closed to exactly the granted set
    reasoning: str

class ScopeSelection(BaseModel):    # build_scope_rules (scope focal, role candidates)
    roles_with_access_names: list[str]
    roles_denied_access_names: list[str]   # explicit prohibitions
    access_is_exclusive: bool              # access to the focal scope is closed to exactly the granted set
    reasoning: str

class Contradiction(BaseModel):
    candidate_name: str
    description: str                  # which policy statements collide; names the kind

class AuditVerdict(BaseModel):
    approved: bool
    reason: str | None = None
    contradictions: list[Contradiction] = []
```

The PRB rebuilds rules from the typed inputs, never from LLM string fields — `ALLOW` from the
granted names, `DENY` from `denied_names ∪ (candidate_set − granted_names if exclusive else ∅)`:

```python
allows = [PolicyRule(role=role, scope=s) for s in scopes if s.name in granted_scope_names]
denies = [PolicyRule(role=role, scope=s, effect=RuleEffect.DENY)
          for s in scopes if s.name in denied_scope_names
          or (grant_is_exclusive and s.name not in granted_scope_names)]
return allows + denies   # allows-then-denies, each in candidate order
```

> **Deny extraction (ALLOW/DENY model).** With two-sided rules in the policy model (`PolicyRule.effect`,
> `RuleEffect.ALLOW` / `DENY` — see [`../policy-model.md`](../policy-model.md)), the PRB emits **both
> grants and prohibitions**. A **DENY** is emitted **only** for an **explicit prohibition** — never for
> mere silence or absence of a grant (those stay deny-by-default non-grants: *no rule at all*). Two
> triggers:
> - **Direct prohibition** about a specific pair — "must not", "cannot", "may not", "is forbidden",
>   "never", "except", "but not", "read-only" → `DENY(focal, that candidate)`.
> - **Exclusivity / restrictive "only"** — closes a set and denies the **complement within the candidate
>   set**: for a focal role, *"developers can **only** access source"* → `ALLOW(dev, source)` +
>   `DENY(dev, X)` for every other candidate scope X; symmetric for a focal scope (*"**only** developers
>   may access source"* → `DENY(role, source)` for every other candidate role).
>
> A single statement may thus yield **both** an ALLOW and one or more DENYs; a **non-exclusive** grant
> imposes nothing on the complement (ALLOW only). Deny/exclusivity extraction is bound by **layer, not by
> source**: it draws on the **scenario layer** — both the scenario `policy.md` prose **and** the
> focal/candidate entity **descriptions** — exactly **symmetric** with the grant side, which already
> reads descriptions (capability projection, Rule 3). A prohibition stated in a role/scope description
> (e.g. *"works … not in source"*, *"does not manage the issue tracker"*) is a valid DENY trigger just
> as a positive description is a valid grant signal. The generic **baseline** (`generic_policy.md`)
> contributes **grants only** and is never a source of denials. The exclusivity complement is
> **derived** from the typed candidate set (never LLM-enumerated:
> an incomplete enumeration would silently re-open the very paths DENY exists to close) and is bounded
> strictly to the current call's candidates — the PRB can only deny what it was handed. A DENY's whole
> purpose is to be a **durable prohibition** that survives a later, broader grant under deny-overrides.

### State fields

```python
class _PRBWorking(TypedDict):
    policy_text: str
    selected_names: list[str]         # granted names (candidate-filtered)
    denied_names: list[str]           # explicit prohibitions (candidate-filtered)
    conflict_names: list[str]         # granted ∩ denied — the contradiction signal
    reasoning: str
    approved: bool
    audit_feedback: str | None
    retry_count: int
    rules: list[PolicyRule]

class RoleRulesState(_PRBWorking):   # role: Role; scopes: list[Scope]
    ...
class ScopeRulesState(_PRBWorking):  # roles: list[Role]; scope: Scope
    ...
```

### Prompts

Lean — task framing, the structured-output contract, two **safety** meta-rules
(**deny-by-default / policy-silence** — grant a pair only if the policy supports it — and
**scope-strictly-to-focal**), and the **deny/exclusivity** rules below. The proposer's task framing
is *"you map access policy to concrete grants **and prohibitions**."*

**Policy-layer labeling.** `_policy_block()` labels the layers so the deny/exclusivity rules bind to
the scenario layer only: `BASELINE POLICY (grants only — never a source of denials):` … then
`SCENARIO POLICY:` …. Correspondingly, `generic_policy.md` is reworded to drop its exclusive tail
(*"…within the domain it is responsible for~~, and nothing outside that domain~~"*) — it still
confines grants to the domain (out-of-domain pairs stay silent non-grants) but contains no
exclusive-language trigger.

**Deny / exclusivity rules** (shared by proposer AND auditor — see share note below):

- **Direct-prohibition** and **exclusivity ("only")** triggers as in the deny-extraction callout
  above; deny extraction is bound to the **scenario layer** (scenario `policy.md` **and** focal/candidate
  descriptions — symmetric with grants; the **baseline** contributes grants only), never source-restricted
  to the policy prose; silence and a non-exclusive grant impose nothing on the complement.
- The two name lists (granted / denied) are **mutually exclusive except** when the policy genuinely
  establishes both a grant and a prohibition for the same candidate (direct conflict or coarse-scope)
  — that overlap is the **contradiction signal**, not a normal proposal.

On top of those, two shared **mapping** rules (`_MAPPING_RULES`) govern how evidence becomes a grant
or a deny:

- **Capability projection (Rule 3, now symmetric)** — a scope names a *set* of operations. **Grant
  side:** any one covered operation established for a candidate grants the whole scope, so partial
  (e.g. read-only) access still earns it. **Deny side (new):** any one covered operation explicitly
  *prohibited* for a candidate denies the whole scope. A coarse scope that is **both** partly
  permitted and partly prohibited for the same pair legitimately lands in **both** lists → surfaced
  as a **contradiction** (a scope-granularity mismatch, not silently resolved).
- **Relationship scoping (Rule 4, amended)** — a policy may state several access relationships over
  the same entities; each grant is judged only by evidence about *that* candidate and the focal
  entity, and a statement about an entity that is neither the focal nor a candidate (even a
  same-theme one) is a different relationship that never counts either way. **One sanctioned
  exception:** exclusive/restrictive scoping **about the focal entity** *is* legitimate evidence to
  deny the complement (that cross-candidate inference is exactly what "only developers" needs).
  Rule 4's protection is otherwise intact for ordinary, non-exclusive multi-relationship statements.

No worked examples or domain heuristics; all substantive reasoning is deferred to the
(user-authored) policy content and the entity descriptions. The **proposer and auditor share the
same rule set** — both make the same grant/deny decision, so a rule on only one side lets the two
diverge (they did: see issue 3.20 *Follow-up: cross-variant convergence*). The auditor adds only its
framing: approve only if every granted pair is policy-supported, every denied pair is a genuine
explicit-prohibition/exclusivity deny, and the exclusivity flag is truly asserted by the scenario
policy — and, when `conflict_names` is present, adjudicate each as a genuine contradiction (→
`contradictions`) vs a proposer generation error (→ ordinary rejection). `build_proposer_messages` /
`build_auditor_messages` carry both name sets (the auditor also gets `conflict_names`).

### LLM + retries

`ChatOpenAI(base_url=LLM_BASE_URL, model=LLM_MODEL, api_key=LLM_API_KEY, temperature=0)`. Two
retry layers, kept distinct:

- **`MAX_AUDIT_RETRIES`** (module constant, default `3`) — the semantic fix-and-retry loop
  between audit and propose.
- **`UPSTREAM_MAX_RETRIES`** (env, default `3`) — tenacity (`stop_after_attempt`, exponential
  backoff, `reraise=True`) around each LLM call for transport failures. The Phase-1 file read
  does **not** retry; it raises directly.

---

## Contradiction contract

The policy model *assumes* no `(role, scope)` is ever both `ALLOW` and `DENY` for the same subject.
The PRB is the producer that must **guarantee** this — it must never pass a contradiction
downstream. Detection and reporting live here; the **treatment** of a reported contradiction (surface
to a human, partial-apply, re-author the policy, split the scope) is a **separate, deferred** task.

> **Read-only pre-commit diagnostic.** The collect-all, quote-bearing form of that treatment — a
> read-only pre-commit tool that surveys a candidate policy, records **all** genuine conflicts at once
> (never aborting on the first), and returns a `ConflictReport` with verbatim quotes — is now specified
> in [`policy-conflict-check.md`](policy-conflict-check.md). It reuses this module's proposer / precheck
> / audit machinery but is a **separate assembly**; the live `/apply` → `PolicyContradictionError` → 422
> path documented in this section is **UNCHANGED** by it.

- **Detection is deterministic** (in `precheck`): `conflict_names = granted_names ∩ denied_names`,
  after candidate-set filtering. Precheck resolves nothing; it only stores the overlap. Because the
  derived exclusivity complement is disjoint from grants by construction, overlap can arise **only**
  from an explicit `denied_names` entry that also appears in `granted_names` — a direct policy
  conflict or a coarse-scope mismatch, exactly the genuine signal we want.
- **Adjudication is by the auditor** (three-way). For each name in `conflict_names` the auditor
  decides whether the policy **genuinely** both grants and prohibits it, or whether it's a proposer
  **generation error**:
  - **Genuine** → the audit node raises `PolicyContradictionError(focal, contradictions)`.
  - **Generation error** → treated as an ordinary rejection: feed the reason back, re-propose,
    reusing the shared `MAX_AUDIT_RETRIES` budget.
- **Report shape.** `PolicyContradictionError` carries `focal: str` and
  `contradictions: list[Contradiction]`, reporting **all** genuine contradictions in a **single**
  raise. **Any** genuine contradiction short-circuits past retry (retrying can't fix a real conflict;
  the call fails closed regardless). Generation errors are **never** reported (LLM noise, not a policy
  finding). The entry-point signature stays `-> list[PolicyRule]`; **the raise is the report**.
- **Fail-closed.** The focal entity's whole rule set is withheld (whether to salvage the
  non-conflicting rules is a treatment decision — deferred).
- **Bounded to the overlap signal.** The PRB is **not** hunting for every latent contradiction in the
  policy independently — only the grant/deny overlap it produced.
- **`Contradiction.description`** names the *kind* — direct policy conflict vs coarse-scope
  granularity mismatch — so the deferred treatment task knows whether to re-author policy or split the
  scope.

---

## Testing

Two layers, distinguished by whether the LLM is real:

- **Mocked-boundary unit tests** (default `pytest`, no marker) — patch `graph._structured_call`
  (the sole LLM seam) and stub `graph.get_policy_source`, so no endpoint is touched. These pin the
  deterministic mechanics: candidate-set precheck/drop, `conflict_names` computation, the three-way
  audit route, the derived exclusivity complement, and allows-then-denies rebuild order. They are the
  fast, hermetic regression net and must stay green with no environment.

- **Live-LLM verification tests** (new **`llm`** marker) — run the **real** LLM defined in the
  environment (`LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`) end-to-end through `build_role_rules` /
  `build_scope_rules`, and assert the emitted rule set matches the policy text. These verify the
  **prompt engineering itself** (that grants, direct-prohibition denies, description-driven denies,
  and the exclusivity complement are extracted correctly), which the mocked tests — feeding canned
  proposer output — cannot.
  - **Only the LLM is real.** Descriptions and policy are **mocked in-process**: inline `Role`/`Scope`
    objects carry the descriptions, and the `PolicySource` seam is stubbed to return an inline policy
    string. No Kubernetes, no Keycloak, no cluster — the `llm` marker gates on the three `LLM_*` vars
    only and **skips cleanly** when they are unset (same pattern as `require_env_or_skip`), so it never
    false-passes and never requires the integration stack.
  - **Assertion:** exact set equality of the emitted `(candidate_name, effect)` pairs against the
    hand-verified expected set for each fixture (not a subset check — an over- or under-grant fails).
  - **Fixture matrix** (minimal but representative): allow-only in **both** directions
    (`build_role_rules`, `build_scope_rules`); a **direct-prohibition** deny ("must not" / "read-only");
    a **description-driven** deny (a prohibition stated only in an entity description, e.g. "does not
    manage the issue tracker"); and an **exclusivity** case ("only …") asserting the derived complement.
    The **contradiction** path (`PolicyContradictionError`) is **excluded** — a real LLM's adjudication
    of a genuine grant/deny collision is non-deterministic and belongs to focused mocked tests.

The `llm` marker is registered in `pyproject.toml` alongside `integration`; unlike `integration` (which
needs the full onboarding stack), `llm` needs only an LLM endpoint. Both are deselected by the default
`-m "not integration"` unit run — the `llm` suite is opt-in via `-m llm` with the `LLM_*` env sourced.

---

## Use-case dispatch

| Use Case | Caller | Function(s) called |
|---|---|---|
| UC1 — Service Onboarding | Service Policy Builder sub-agent | `build_scope_rules(other_roles, scope)` per agent/tool scope + `build_role_rules(role, other_scopes)` per agent role (agent path only) |
| UC2 — Policy Update (Build) | Build sub-agent | TBD |
| UC3 — Role Update | Role sub-agent | `build_role_rules(role, all_scopes)` — one call |
| Policy Conflict Check (diagnostic) | Controller (`POST /policy/check`) → `check_policy_conflicts` | A parallel diagnostic assembly reusing propose / precheck / audit (record-not-raise + a terminal `explain` node) — see [`policy-conflict-check.md`](policy-conflict-check.md) |

---

## Configuration

| Variable | Used for | Phase |
|---|---|---|
| `AIAC_POLICY_FILE` | Path to the whole-file access policy (default `/etc/aiac/policy.md`) | 1 |
| `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY` | LLM calls | 1 |
| `UPSTREAM_MAX_RETRIES` | Transport retry budget for LLM (and, in Phase 2, ChromaDB) calls (tenacity, default `3`) | 1 |
| `AIAC_CHROMADB_URL` | ChromaDB endpoint | 2 |
| `CHROMA_N_RESULTS` | Number of results per ChromaDB query (default `10`) | 2 |

`MAX_AUDIT_RETRIES` (default `3`) is a module constant, not an env var.
