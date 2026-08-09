# Component Sub-PRD: Policy Hygiene

> **Depends on:** [policy-guardrails-agent.md](policy-guardrails-agent.md) — module placement, the verification contract, the LangGraph design, the shared findings/verdict model, and Configuration. This sub-PRD specifies **Check 1 (Policy hygiene)** of the `policy` API family only; it does not restate the parent's architecture.

## Scope

Policy hygiene is the **intrinsic, single-document** check the Policy Guardrails Agent runs against one incoming `aiac-policies` document. It judges whether the document is a well-formed, actionable, internally consistent access-control statement — independent of any other document.

It runs **identically for both `replace` and `update`** operations (hygiene is operation-independent; only Check 2, contradiction, depends on operation). It is the first node in the parent's `StateGraph`; a blocking hygiene finding short-circuits straight to `verdict`, so contradiction is never evaluated on malformed text.

### Intrinsic-only boundary (fixed constraint)

The agent's configuration exposes only `CHROMA_URL` and the LLM trio — **no IdP access**. Hygiene therefore judges **intrinsically, from the document text alone**:

- It does **not** read ChromaDB (that is Check 2's `corpus_fetch`).
- It does **not** resolve subjects/roles/services/scopes against Keycloak. "Actionable / translatable" means the text *names a plausible* subject/role → service/scope/action, **not** that those identifiers actually exist in the IdP. Whether the referenced entities resolve is the downstream AIAC Agent's concern, not hygiene's.

### Deferred (out of scope this increment)

- **Prompt-injection / adversarial-content screening** — always a separate concern from hygiene (see parent).
- **Corpus contradiction and intra-batch contradiction** — Check 2 and a documented parent deferral, respectively.

## The three facets

Hygiene evaluates three facets of a document's intrinsic quality. Each facet maps to one or more finding codes (next section):

1. **On-topic & well-formed** — the document must actually express access-control intent (some subject/role → service/scope/action). Empty, gibberish, or off-topic text fails.
2. **Actionable / translatable** — the statement must be specific enough for the downstream AIAC Agent LLM to emit Rego: it references resolvable-*looking* roles/services/scopes/actions and avoids hopeless vagueness.
3. **Internally consistent & clear** — the document does not contradict *itself* and is not so ambiguous it would yield nondeterministic Rego. (Self-contradiction here is distinct from corpus contradiction, which is Check 2.)

## Finding taxonomy

Hygiene emits findings from a **closed vocabulary of codes**. Each code has a **fixed severity** — the degree distinction (e.g. hopeless vs. minor vagueness) is encoded by *which code* is chosen, not by a per-finding severity. This makes the verdict deterministic given the set of codes.

| Code | Facet | Severity | Definition | Example |
|------|-------|----------|------------|---------|
| `NOT_A_POLICY` | on-topic / well-formed | **blocking** | Empty, whitespace-only, gibberish, or text that expresses no access-control intent (off-topic). | *"The cafeteria menu changes every Friday."* (off-topic; no subject → action) |
| `UNTRANSLATABLE` | actionable | **blocking** | Expresses intent but is too vague to yield Rego — no resolvable subject/role or service/scope/action. | *"Systems should be secure and do the right thing."* |
| `SELF_CONTRADICTION` | consistent | **blocking** | The document contradicts *itself* (two statements in the same document cannot both hold). | *"Admins may delete any record. Admins may never delete records."* |
| `AMBIGUOUS` | consistent | **blocking** | Admits multiple irreconcilable readings, such that translation to Rego would be nondeterministic. | *"Users in group A or B with the admin role may access billing."* (does "admin role" qualify both groups, or only B?) |
| `TOO_LARGE` | operational | **blocking** | Document exceeds `GUARDRAILS_MAX_DOC_CHARS`. An operational bound, not a semantic judgement — the text may well be valid policy that is simply too large to reason over reliably. | — (any document longer than the char ceiling; no snippet applies) |
| `UNDERSPECIFIED` | actionable | advisory | Minor vagueness; still translatable but could be tightened (e.g. an unqualified action verb). | *"Support staff can update tickets."* (translatable, but the "update" scope is loose) |
| `REDUNDANCY` | consistent | advisory | The document repeats the same access-control statement within itself. | *"Editors may publish articles. Editors are allowed to publish articles."* |
| `STYLE` | cross-cutting | advisory | Wording / formatting nits that do not affect meaning. | *"editors CAN publsih articles!!!"* (typo / formatting; meaning intact) |

**LLM-emittable vs. guard-only.** The LLM's structured output is constrained to the **seven semantic codes** (all except `TOO_LARGE`). `TOO_LARGE` is emitted only by the pre-LLM guard and never appears in LLM output. `NOT_A_POLICY` is emitted by *both* paths — the guard for empty/whitespace input, the LLM for gibberish/off-topic.

> **Note.** The `Example` column gives one illustrative snippet per code to sharpen its definition (`TOO_LARGE` is a size bound, so no snippet applies). These are definitional anchors, **not** a calibration corpus — a larger good/bad example set for tuning the blocking/advisory threshold remains a separate rubric/eval artifact (see [Open decisions](#open-decisions-tbd)).

## Severity and verdict

Severity is **derived by the agent from the code**, not emitted by the LLM. The LLM emits only `{code, message}`; the agent looks up severity from the fixed table above. This prevents the LLM from contradicting the taxonomy (e.g. marking a `STYLE` finding blocking).

- **Verdict = reject** iff at least one finding has a derived severity of **blocking**; otherwise **accept**. (This is the parent's rule; hygiene simply feeds blocking findings into it.)
- **Advisory findings never block.** They are returned alongside the verdict so the operator sees non-fatal concerns, but they do not fail the request — important given the parent's all-or-nothing semantics, where one false blocking finding fails the operator's whole ingest request.
- **Emit-all.** In its single call the LLM returns **all** applicable findings, including advisories, even when a blocking finding is also present. It does not stop at the first problem.

## Processing flow

Hygiene is the parent graph's `hygiene` node. Internally it is two stages:

1. **Pre-LLM guard (deterministic, no LLM call):**
   - Empty or whitespace-only text → single `NOT_A_POLICY` finding; return immediately.
   - Text length > `GUARDRAILS_MAX_DOC_CHARS` → single `TOO_LARGE` finding; return immediately.
   - These paths are reproducible and cost nothing, and (being blocking) trigger the parent's short-circuit to `verdict`.
2. **LLM call (one call, all three facets):** the document text is judged against the three facets in a single structured-output call. The LLM returns zero or more `{code, message}` findings drawn from the seven semantic codes. The agent then derives severity per finding and hands the findings to the parent's verdict aggregation.

The hygiene node never populates `related_doc_id` — that field is reserved for Check 2 (contradiction), which names a conflicting corpus document. Hygiene is intrinsic and has no peer document to reference.

Determinism: the hygiene LLM call is issued at a **low (pinned) temperature** to keep verdicts as reproducible as an LLM boundary allows.

## Structured-output contract

The LLM call uses the LangChain structured-output surface (parent: `with_structured_output`) so findings are validated at the call boundary. The model the LLM must satisfy:

**`HygieneFinding` (LLM-emitted):**

| Field | Type | Notes |
|-------|------|-------|
| `code` | enum | One of the **seven** semantic codes (`TOO_LARGE` excluded — guard-only). |
| `message` | str | Specific, actionable, one-sentence explanation. Should point at the offending aspect so an operator can fix the document. |

**`HygieneResult` (LLM-emitted):**

| Field | Type | Notes |
|-------|------|-------|
| `findings` | `list[HygieneFinding]` | All applicable findings; empty list ⇒ the document is clean. |

The agent maps each `HygieneFinding` into the parent's shared finding shape by adding the fixed fields: `check = "hygiene"`, `severity` (derived from `code`), and the LLM's `message`. `related_doc_id` is omitted.

## Prompt responsibilities

(Literal prompt text is an implementation artifact, out of scope for this spec — see the parent's no-prompt-text convention.) The hygiene system prompt must convey:

- The agent's role: judge one candidate `aiac-policies` document for intrinsic quality only.
- The three facets and the seven semantic codes with their definitions and fixed severities.
- The instruction to emit **all** applicable findings (blocking and advisory together), or an empty list when clean.
- The structured-output shape (`HygieneResult`).
- The intrinsic-only boundary: judge from the text alone; do not assume access to a corpus or to the IdP; do not require that named roles/scopes actually exist — only that they are named plausibly.

Few-shot examples and the exact blocking/advisory boundary phrasing are calibration concerns (see [Open decisions](#open-decisions-tbd)).

## Configuration

Adds one variable to the Policy Guardrails Agent's configuration (parent Configuration table), alongside the existing `GUARDRAILS_MAX_CORPUS_DOCS`:

| Variable | Default | Source | Purpose |
|----------|---------|--------|---------|
| `GUARDRAILS_MAX_DOC_CHARS` | `20000` | ConfigMap | Upper bound on a single document's character length. Exceeding it yields a deterministic `TOO_LARGE` rejection with no LLM call. |

`LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY` are reused from the parent (the same trio the AIAC Agent uses); hygiene requires no additional LLM configuration.

## Testing decisions

**Seams:** the deterministic guard and the severity-derivation/verdict aggregation need **no LLM** and are unit-testable directly; the LLM call is tested with a mocked structured-output response.

Key behaviors to assert:

- **Guard:** empty and whitespace-only text yield exactly one `NOT_A_POLICY` finding with **no LLM call**; text over `GUARDRAILS_MAX_DOC_CHARS` yields exactly one `TOO_LARGE` finding with no LLM call.
- **Severity derivation:** each code maps to its fixed severity; the LLM's output carries no severity and cannot alter it. A `STYLE`/`REDUNDANCY`/`UNDERSPECIFIED` finding never produces a reject; any of the five blocking codes does.
- **Verdict aggregation:** verdict = reject iff ≥1 blocking finding; advisories alone ⇒ accept; empty findings ⇒ accept.
- **Emit-all:** a mocked response mixing blocking and advisory findings preserves all of them in the result (advisories are not dropped when a blocking finding is present).
- **Contract:** LLM output constrained to the seven semantic codes; `TOO_LARGE` never appears in LLM output; hygiene findings never carry `related_doc_id`.
- **Operation independence:** hygiene produces identical findings for `replace` and `update` given the same text.

## Open decisions (TBD)

1. **Strictness calibration** — how conservative the prompt is about emitting *blocking* vs. *advisory* findings (parent open-decision #2). This sub-PRD is the home for that decision but does not yet resolve it; the code→severity table fixes the *mechanics*, while the LLM's threshold for choosing, e.g., `UNTRANSLATABLE` (blocking) over `UNDERSPECIFIED` (advisory) is deferred to prompt-tuning against real examples.
2. **Calibration corpus** — a larger good/bad example set (beyond the one-per-code snippets in the taxonomy table) for tuning the blocking/advisory threshold. Deferred; this is a separate rubric/eval artifact, not this design spec.
3. **`GUARDRAILS_MAX_DOC_CHARS` default** — `20000` is a starting recommendation; the right ceiling depends on the chosen model's context budget and realistic policy-document sizes.
