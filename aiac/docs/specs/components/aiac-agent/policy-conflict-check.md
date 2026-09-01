# Sub-PRD: AIAC Agent — Policy Conflict Check (pre-commit diagnostic)

> **Depends on:** [`../aiac-agent.md`](../aiac-agent.md) — Controller, Shared Module, Configuration, Error Handling, Runtime.

> **Sits next to the live PRB contradiction path.** This diagnostic reuses the Policy Rules Builder machinery specified in [`policy-rules-builder.md`](policy-rules-builder.md), but is a **separate, read-only** path. The live `/apply` → `PolicyContradictionError` → HTTP 422 contract documented there is **UNCHANGED** by this feature.

## Description

The **Policy Conflict Check** is a **read-only pre-commit diagnostic** a caller invokes **before**
onboarding/committing a policy, to discover grant/prohibit contradictions ahead of time. Given a
candidate `policy_text` and a target service, it surveys **that service's** focal entities against
the live IdP catalog, runs the **same** proposer → precheck → audit machinery the live path uses,
and — instead of aborting on the first genuine contradiction the way `/apply` does — **records every
genuine contradiction and continues**. It returns a single `ConflictReport` listing **all** conflicts
at once (each with verbatim, substring-validated quotes, a plain-language explanation, and a conflict
`kind`), or the rendered form of `status: no_conflict` ("No conflict.").

This is a **diagnostic gate**, not the commit. It **never mutates policy state**, never calls the PCE
(`compute_and_apply`), and never blocks anything by itself; the **caller decides** what to do with the
report. It does **not** replace or modify the live `/apply` → 422 path.

---

## Interface

- **New Controller route** — `POST /policy/check` (**recommended**; the exact path is a #154 open item,
  `/policy/check` vs `/policy/conflicts`). The route is a **thin serialization shell** over a testable
  plain function:

  ```python
  def check_policy_conflicts(policy_text: str, service_id: str) -> ConflictReport: ...
  ```

  This mirrors how the repo separates graph logic from the `/apply` route, keeping the LLM-heavy logic
  drivable by the `-m llm` suite without HTTP.

- **Inputs:**
  - **`policy_text`** — the **candidate prose supplied directly as an argument**, *not* read from
    `AIAC_POLICY_FILE`. The diagnostic assembly's first node injects the supplied text instead of the
    live graph's file-reading fetch.
  - **`service_id`** — the target service's **Keycloak internal client UUID** (`s.id == service_id`),
    matching `POST /apply/service/{service_id}`.

- **Catalog resolution — live from the IdP.** Roles/scopes are resolved **live from the IdP** for the
  target service — the **same** `own_scopes` / `candidate_roles` / `other_scopes` resolution
  `builder.py` already performs (see [`uc1-service-onboarding.md` → Service Policy Builder](uc1-service-onboarding.md#sub-agent-service-policy-builder)).
  This is deliberately **not** a text-only endpoint: conflicts are structural (same role + same scope),
  which only means something against the real, typed entity set. It reuses `_precheck`'s hallucination
  filtering so every reported entity is real.

- **Scope of one check — per-service.** "All conflicts" = all conflicts involving **that service's**
  focal entities, matching the commit unit (you onboard one service at a time). The whole-policy loop
  (survey every already-onboarded service) is **deferred** — see **Out of scope**.

---

## Reused machinery + code-contact deltas (D11–D14)

The diagnostic is a **separate assembly** that reuses `_propose` / `_precheck` / the proposer +
auditor prompts **unchanged**, swaps in a **record-not-raise** audit node, and adds a **terminal
`explain` node**. A separate **sequential** survey orchestrator drives the per-entity units alongside
`builder.py`'s loop. Four code-contact points make the "reuse unchanged" framing plannable against the
current PRB code:

- **D11 — the structured `kind` is produced by the new `explain` node, not read from the auditor.**
  In the current code the conflict *kind* is not a typed value — it exists only as prose inside
  `Contradiction.description`; `_precheck` collapses both kinds into one overlap signal. So the
  `explain` call **classifies** `kind ∈ {direct, coarse_scope}` itself, from the policy text + the
  `(role, scope)` pair, with the auditor's own `description` passed in as a **hint**. Chosen over adding
  a `kind` field to the shared `Contradiction` / `AuditVerdict` models, which would touch the live
  `/apply` path that D1/D8 require to stay byte-for-byte unchanged. (The auditor's `description` — the
  adjudicator's own conclusion, not the proposer's `reasoning` that D9 excludes — is a safe hint.)
- **D12 — name→id join + run-direction tagging in the engine.** Audit output carries only **name
  strings** (no ids, no typed objects). The survey orchestrator re-joins each name to the resolved
  entity set (D13) to recover the id, and tags each recorded conflict with its **run direction** at
  record time — scope-focal run ⇒ focal is the scope, candidate is the role; role-focal run ⇒ focal is
  the role, candidate is the scope — which is how the report's `role(name+id)` / `scope(name+id)` sides
  are assigned.
- **D13 — prerequisite refactor: extract a standalone resolver.** Focal-entity resolution is currently
  inlined in `ServicePolicyBuilder.build()` and entangled with the fan-out loop. Extract it into a pure
  `resolve_focal_entities(service_id, service_type) -> FocalEntitySet` (typed: `own_scopes`,
  `own_roles`, `candidate_roles`, `other_scopes`), and have **both** the live `build()` and the
  diagnostic call it. This is a **pure extraction — no behavior change to the live path** (covered by
  existing builder tests). The existing `HTTPException(502/404)` on IdP-unreachable / unknown-service
  moves into the resolver and directly satisfies this feature's pre-survey HTTP boundary.
- **D14 — "separate assembly" = fork the audit node + build a parallel graph; there is no swap seam.**
  The raise is hard-coded in `_audit` with no strategy seam, and the live START node (`_fetch`) calls
  the hard-coded `get_policy_source()` and overwrites any input `policy_text`. So there is **NO
  node-swap / source-injection seam**. The diagnostic instead uses: (i) a START node that **seeds
  `policy_text` from input state** (no file read); (ii) `_propose` / `_precheck` reused unchanged;
  (iii) a forked `_audit_diagnostic` that **records** contradictions into state instead of raising;
  (iv) a terminal `_explain` node (D11). The live `graph.py` `_audit` and the `build_*` entry points
  stay **untouched**.

---

## Settled design decisions

(Condensed from handoff 04 §4, decisions D1–D10.)

- **D1 — Separate diagnostic, live path untouched.** A read-only pre-commit tool, distinct from
  `/apply`. The live commit path keeps raising `PolicyContradictionError` → 422.
- **D2 — Text + live IdP catalog** (not text-only). A structural conflict definition requires a real
  typed entity set; text-only extraction cannot dedupe/validate entity names or drop hallucinations,
  which is exactly wrong for a gate.
- **D3 — Provenance is conflict-only.** Clean rules carry **no** citation. Only auditor-confirmed
  genuine conflicts get quotes, produced by a dedicated explanation prompt. Many rules have no quotable
  statement (description-derived grants, exclusivity-complement denies, deny-by-default), so forcing
  per-rule citations would maximize hallucination where there's nothing to cite.
- **D4 — Explain only auditor-confirmed genuine conflicts** — not every `_precheck` overlap. The
  auditor's genuine-vs-slip adjudication keeps proposer noise out of the report; slips still trigger the
  normal retry (which usually erases the bogus overlap).
- **D5 — Verbatim + validated quotes.** The explanation prompt returns exact substrings of the policy
  text; the tool checks each is a substring (whitespace-normalized). Each side is a **list of spans**
  ("one or more" statements). On validation failure: **keep** the conflict, set `quotes_verified=false`,
  fall back to the auditor `description`.
- **D6 — Report both conflict kinds:** `direct` **and** `coarse_scope` granularity. A coarse-scope
  contradiction ("management granted, writing forbidden") is genuine and more insidious; the machinery
  already distinguishes the two, so it is nearly free.
- **D7 — Collect-all survey, never abort.** Run **every** focal entity to completion; accumulate
  conflicts + un-evaluatable entities. The audit node in diagnostic mode **records** genuine
  contradictions instead of raising.
- **D8 — Separate diagnostic assembly**, reusing `_propose` / `_precheck` / prompts unchanged; swap in a
  record-not-raise audit node + a new terminal `explain` node; a separate survey orchestrator alongside
  `builder.py`'s loop. A `diagnostic: bool` flag *inside* the live audit node was **rejected** — it
  risks a conflict that should 422 a commit silently becoming a recorded-and-ignored report.
- **D9 — Explanation prompt: one call per conflict pair.** Inputs = policy text + the one
  `(role, scope)` pair + the auditor's kind label. **Not** the proposer's free-form `reasoning` (LLM
  narration that could anchor extraction onto a hallucinated justification). Conflicts are rare
  (usually 0), so per-pair isolation gives the cleanest verbatim extraction with no cross-pair
  contamination.
- **D10 — Identity by entity id; no cross-run reconciliation.** The fan-out is disjoint (scope-focal =
  candidate role × own scope; role-focal = own role × other-service scope; own vs. other split by
  `serviceId`), so the same pair is never decided twice. A conflict is always a within-single-run
  overlap.

---

## Pipeline (diagnostic assembly)

Per focal entity:

```
inject candidate policy_text  (START seeds text from input; replaces the file-reading _fetch)
  → _propose         (reused unchanged)
  → _precheck        (reused unchanged; flags candidates in both the grant and prohibit lists)
  → _audit_diagnostic:
        genuine contradiction  → RECORD (do NOT raise), continue
        proposer slip          → ordinary rejection → re-propose (≤ MAX_AUDIT_RETRIES)
        retry budget exhausted → mark entity UNEVALUATED (do NOT raise)
  → _explain (terminal; runs only if this entity has recorded genuine conflicts):
        one LLM call per (role, scope) pair
        → granting_quotes[], prohibiting_quotes[]   (verbatim, validated substrings)
        → explanation, kind  (kind classified here — D11)
        → quotes_verified   (false ⇒ fall back to the auditor description)
```

A **sequential** survey orchestrator (alongside the onboarding builder's loop) runs **every** focal
entity to completion — the first conflict never aborts — then assembles the report. Concurrency is
deferred (see **Out of scope**). A CHECK-node touch on the component Mermaid diagram is **optional** and
noted here rather than forced into that diagram.

---

## Report + status contract

```
ConflictReport:
  conflicts:   [{ focal:  { name, id, type },        # type ∈ {"role", "scope"} — which side drove the run
                  role:   { name, id },
                  scope:  { name, id },
                  kind,                                # kind ∈ {"direct", "coarse_scope"}
                  granting_quotes:   list[str],        # verbatim substrings of policy_text
                  prohibiting_quotes: list[str],       # verbatim substrings of policy_text
                  explanation,
                  quotes_verified }]
  unevaluated: [{ focal:  { name, id, type },
                  reason,                              # enum { "nonconvergence" }
                  detail }]                            # optional free-text
  status: "no_conflict" | "conflicts_found" | "incomplete"
```

### Status enum + precedence (RESOLVED — authoritative)

The status is derived by this precedence, exactly:

```
if conflicts:                              -> conflicts_found
elif evaluated_count == 0 or unevaluated:  -> incomplete
else:                                      -> no_conflict
```

Both `incomplete` disjuncts are **load-bearing**: `evaluated_count == 0` catches the empty-input /
zero-focal case; `unevaluated != []` catches retry-exhaustion on some entities while the rest are clean.
`no_conflict` is reached **only** when ≥1 entity was evaluated **and** `conflicts == []` **and**
`unevaluated == []`. The literal string "No conflict." is the *rendered* form of `no_conflict` — never a
bare string that could mask an incomplete run (guards against an outage looking identical to a clean
policy).

### Quote-validator rule

**Whitespace-normalize ONLY** before the substring check: collapse all whitespace runs (including
newlines) to a single space and trim the ends. Deliberately **no** case-folding and **no** smart-quote /
punctuation normalization — the quote must be findable *as written* in the author's prose; normalizing
punctuation would let a non-findable near-quote pass. On mismatch (including smart-quote mismatches):
**keep** the conflict, set `quotes_verified=false`, and fall back to the auditor `description`. Never a
silent "fix." `granting_quotes` / `prohibiting_quotes` are `list[str]` of verbatim substrings — not
offset spans (nothing consumes offsets).

### HTTP status boundary

- **Pre-survey failure** — can't build the entity set (IdP unreachable, unknown service, missing
  `policy_text`) ⇒ **non-2xx**, **no report** (502 for upstream IdP; 400/422 for bad input). The D13
  resolver's existing `HTTPException(502/404)` satisfies this directly.
- **Per-entity failure during the survey** — an entity won't converge / exhausts retries ⇒ **200**,
  entity listed under `unevaluated`, status ≠ `no_conflict`.
- **Any completed survey** — conflicts or clean, possibly partial ⇒ **200, never 422**. A found
  conflict is a *successful diagnosis*, not an error — unlike the live `/apply`, this tool does **not**
  422 on conflict. This is the controller's **first JSON response body** (existing `/apply/*` routes
  return bare status codes).

### Report boundary note

`status == no_conflict` means "no conflict introduced by **this service's** rules," **not** "the global
policy is conflict-free." This is documented in the report so callers don't over-read a per-service
result.

---

## Testing

Three tiers, mirroring the repo's existing split (deterministic unit tests patch the `_structured_call`
seam; the opt-in `-m llm` suite runs the real model — see [`policy-rules-builder.md` → Testing](policy-rules-builder.md#testing)):

1. **Deterministic unit (patch `_structured_call`)** — the bulk. The shared `_structured_call` LLM seam
   already covers proposer, auditor, **and** the new per-pair explanation call (a `side_effect` list
   drives all three), so no new patch seam is introduced. Cover: survey **orchestration** (all entities
   run; first conflict does **not** abort), conflict → **report assembly**, the **`unevaluated` path** on
   retry exhaustion, the **zero-evaluated guard**, **`status` derivation** (the precedence above), and
   the **verbatim-quote validator** as a pure function (substring match, whitespace normalization,
   `quotes_verified=false` fallback to `description`).
2. **Live-LLM (`-m llm`)** — small, **blatant** planted fixtures: one **direct** conflict, one
   **coarse-scope** conflict, and one **clean** policy. Assert **structural** properties only: clean ⇒
   `no_conflict`; planted ⇒ the confirmed set **contains** the planted pair(s), and **every** returned
   quote is a verbatim substring of the policy text. **Do not** assert exact quote strings or explanation
   wording (model nondeterminism; convergence on subtle prose is known-fragile).
3. **Route** — one thin test that the endpoint calls the survey function and serializes report + status
   codes (function patched, no LLM).

---

## Out of scope

- **Whole-policy audit** (survey *every* already-onboarded service): desired later as a **loop over the
  per-service check** — a **separate effort**, not this work.
- **Survey concurrency:** assume **sequential** (matches `builder.py`); parallelize only if latency
  demands.
- **Route path name:** decide at implementation (`/policy/check` vs `/policy/conflicts`).
- **Subtle-prose robustness / PRB precedence tuning** (explicit prohibition vs description-derived
  grant): a separate PRB-quality concern, not a correctness gate for this feature.
- **ALLOW-vs-DENY precedence / tie-break at enforcement time:** a distinct PCE/Rego concern (tracked in
  #124). This tool *reports* contradictions; it does not *resolve* them.
- **No change to the live `/apply` path** or its 422 contradiction contract.
- **Provenance on non-conflicting rules:** descoped — clean rules carry no citation.

---

## Acceptance criteria

(Carried over from #154 / handoff 04 §9.)

1. `POST` with a clean candidate policy + a real service ⇒ 200, `status: no_conflict`.
2. `POST` with a candidate policy containing a **direct** and a **coarse-scope** conflict ⇒ 200,
   `status: conflicts_found`, **both** reported in a single response, each with validated verbatim
   `granting_quotes` / `prohibiting_quotes` (or `quotes_verified=false` + description fallback).
3. An entity that cannot converge appears under `unevaluated` with a reason; the run still returns 200
   and does **not** report `no_conflict`.
4. IdP unreachable / unknown service / missing `policy_text` ⇒ non-2xx, no report.
5. Zero entities evaluated ⇒ `status: incomplete`, never `no_conflict`.
6. The live `/apply` path and its 422 behavior are unchanged (regression check).
7. Deterministic unit tests cover orchestration, report assembly, `unevaluated`, the zero-evaluated
   guard, and the quote validator/fallback; the `-m llm` suite asserts containment + substring-validity
   on planted/clean fixtures.
