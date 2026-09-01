# AIAC Evaluation Framework

## 1. Purpose

This document specifies an evaluation framework for AIAC, designed independently
of the evaluation suite that previously lived at `test/integration/eval/` and
has since been relocated to `aiac/eval/` (see §11) (the "legacy suite"). The
legacy suite's *implementation* — Keycloak
provisioning helpers, the `opa eval` invocation wrapper, the synthetic
Role/Scope fixture builder, and its report-generation `conftest.py` hooks —
is reused wherever it already covers something this spec calls for; the
*design* (what gets measured, how it's scored, what gates what) is derived
fresh from AIAC's own semantics, not inherited from the legacy suite's
assumptions.

The framework targets four quality attributes:

| Attribute | Question it answers |
|---|---|
| **Correctness** | Does the output comply with the natural-language policy? |
| **Robustness** | Does a small, meaning-preserving change to the policy leave the result unchanged — and does a small, meaning-changing edit change it correctly? |
| **Scale** | Does AIAC hold up under a large number of agents, tools, and entitlements? |
| **Consistency** | Does AIAC reproduce identical results across repeated runs on identical input? |

## 2. Scope: what is under test, and at what level

AIAC's LLM-driven reasoning lives entirely in the **Policy Rules Builder
(PRB)** — the LangGraph pipeline that decides, one role-vs-candidate-scopes
(or scope-vs-candidate-roles) call at a time, which grants to make. Everything
downstream (the Policy Computation Engine merging rules into
`ServicePolicyModel`s, and the PDP Policy Writer rendering Rego) is
deterministic Python with no policy interpretation of its own.

Every attribute in this framework is evaluated at **two levels**:

- **PRB-level** — the PRB's raw `list[PolicyRule]` output, checked directly
  against ground truth. Isolates the LLM's policy-interpretation reasoning
  from everything downstream. Fast and cheap enough to be the default/gated
  tier.
- **End-to-end** — the full pipeline through the Policy Computation Engine,
  real OPA, and rendered Rego, checked via `opa eval` against the same ground
  truth. The only way to catch integration bugs between layers (merge logic,
  Rego rendering, OPA semantics) that a PRB-only check can't see.

### 2.1 Backend pluggability (PRB-level only)

The PRB-level harness supports **two interchangeable sources** for the
`Role`/`Scope` objects it feeds the PRB:

- **Synthetic** — in-memory objects constructed directly (no Keycloak), the
  default for the full PRB-level matrix (Correctness, Robustness,
  Consistency, Scale) across every scenario. Fast, cheap, gates routine runs.
- **Real Keycloak** — objects sourced from a live-provisioned realm, used
  only as a **periodic fidelity check**, run against the primary Correctness
  corpus only (not the full Robustness/Scale matrix). Its job is narrowly
  "confirm the synthetic fixtures still match what real Keycloak actually
  returns," not to re-derive the correctness signal a second time.

The end-to-end level always uses real Keycloak + real OPA; there's no
synthetic variant at that level, since its entire purpose is catching
integration bugs the synthetic path can't produce.

## 3. Correctness

**Ground truth:** hand-authored truth tables, one per scenario, written by a
human alongside the scenario's natural-language policy document. This is the
highest-trust method available and is used for the primary Correctness and
Robustness corpora (see §7 for why Scale uses a different method).

**Scoring:** the actual grant set (PRB-level `list[PolicyRule]`, or
end-to-end `opa eval` results) is compared against the truth table via
**precision and recall, tracked and reported separately** — never blended
into a single F1-style score. This is an access-control system, where the
two error types are not equivalent:

- **Over-grants** (false positives — a grant present that shouldn't be) are
  the security-critical failure mode: an accidental grant is a live
  excess-privilege vulnerability the moment it's provisioned.
- **Under-grants** (false negatives — a grant missing that should be
  present) are the availability failure mode: a support ticket, not a
  vulnerability.

**Gate:** **zero tolerance on over-grants** — any false-positive grant fails
the scenario outright. Under-grants are reported and tracked but held to a
looser threshold (exact threshold TBD at implementation time; not zero
tolerance).

**Corpus design:** scenario themes are derived fresh from AIAC's own stated
policy semantics (per the PRD — deny-by-default, most-restrictive-reading-
wins, the delegation model, wildcard expansion, etc.), then **cross-checked
against the legacy suite's existing taxonomy afterward** (ambiguity
resolution, wildcard expansion, adversarial/misleading naming, empty
descriptions, identity/boundary confusion, delegation, prompt injection,
direct contradiction) to catch any gap either derivation missed on its own.
The cross-check is a validation step against an existing list, not a
starting point.

## 4. Robustness

Robustness is only meaningful when measured *against a correctness oracle
that is itself perturbed*, not by checking "did the output change at all."
A model that ignores the policy text and always emits the same grants would
score perfectly on a naive robustness check while being useless. This
framework therefore builds robustness scenarios as **matched pairs**, one of
each type per original scenario, scored as **two separate metrics** (never
blended):

1. **Invariance family** (equivalence-preserving perturbation) — the policy
   is reworded/reformatted but its meaning is unchanged, so ground truth is
   *identical* to the original. Pass = output unchanged from the original's
   correct grant set.
2. **Sensitivity family** (meaning-changing minimal edit) — a small but
   meaning-changing edit (e.g. "may access" → "may not access", a role name
   swapped, an exception clause added, a restriction word ("only", "just")
   inserted or removed to narrow or broaden an otherwise identical grant), so
   ground truth is *deliberately different*. Pass = output changes, in the
   predicted direction. This is the control that proves the system isn't just
   numb to its input — without it, "robust" and "broken" are
   indistinguishable.

Both families use **two perturbation tiers**:

- **Mechanical** — whitespace, casing, punctuation noise, candidate-list
  reordering. Generated programmatically; deterministically guaranteed to be
  meaning-preserving (or, for the sensitivity family, deterministically
  constructed to flip a specific known fact).
- **Semantic** — full paraphrase / rewording. May be **LLM-drafted**, but
  every semantic perturbation **requires human sign-off** confirming it
  actually preserves (or changes, for the sensitivity family) meaning as
  intended before it enters the corpus. The human review is the actual
  ground-truth authority; the LLM only saves drafting time.

## 5. Scale

"Large number of agents, tools, entitlements" decomposes into two
independent dimensions that stress different parts of the system, and both
are measured on **both check types**, at **both levels** (§2):

| Dimension | What it stresses | Check types |
|---|---|---|
| **Total-corpus scale** | Many roles/scopes/services overall, each individual PRB decision still facing a modest candidate list. Stresses the deterministic merge engine, Rego document size, OPA eval latency, total wall-clock/cost across many PRB calls. | Structural (completeness, no duplication/orphans, latency, cost) **and** Correctness (§3 metric) |
| **Per-decision scale** | A single role/scope facing a very large candidate list in one PRB call. Stresses the LLM itself — context pressure, needle-in-a-haystack attention degradation — and can directly hurt correctness exactly where the over-grant gate is least tolerant. | Structural **and** Correctness (§3 metric) |

A system can pass one dimension and silently fail the other (e.g. handle
10,000 total entitlements fine because no single decision is large, but
degrade the moment one role has 200+ candidate scopes), so they are never
collapsed into one "scale score."

**Ground truth:** hand-authored truth tables don't scale past low double
digits of entities, so Scale uses **procedurally generated policy+truth
pairs** — policies built programmatically from a template/grammar so the
generator knows the ground truth by construction, then rendered as NL
description. Structural checks (completeness, no orphans/duplication) don't
require per-entity truth verification at all; the correctness checks at
scale rely on the generated ground truth rather than human review, since
nobody can hand-verify hundreds of entities.

**Tiers:**

- **Fixed regression target: 100 services.** A fixed-size corpus (both
  dimensions, both check types, both levels) intended as a stable regression
  guard once cadence is turned on (see §8). Anchored below the PRD's only
  documented scale target ("hundreds of services," per
  `docs/specs/components/policy-model-store.md:171`) as a starting point;
  revisit upward as confidence grows.
- **Exploratory breaking-point tier.** Geometric scale-up (e.g. 10 → 100 →
  1,000...) on both dimensions, **hard ceiling of 1,000 entities**, to find
  the actual ceiling and characterize the failure mode (crash, timeout,
  silent correctness degradation, cost blowup). Not pass/fail — produces a
  curve/report. **Manual-invocation only**, never automatic, so cost
  exposure from open-ended scaling is always a deliberate choice.

## 6. Consistency

**Method:** run the PRB (PRB-level, synthetic backend) **5 times** on
identical input, scoped to the **primary Correctness corpus only** — not the
Robustness matched pairs, not the Scale corpus, both of which would multiply
cost substantially for a claim ("is the PRB deterministic") that the primary
corpus already carries.

**Gate:** **zero tolerance** — exact grant-set equality is required across
all 5 runs; any single disagreement fails. This is access control; "usually
reproducible" is not a real guarantee.

**Trend reporting:** even on a passing run, the disagreement rate (if any)
is recorded to the trend log (§9) so an occasional flake that stays under
the gate doesn't silently worsen over time without anyone noticing. This
also acknowledges that LLM APIs at temperature=0 are not literally
guaranteed bit-identical across calls (provider-side batching, nondeterministic
kernels), so the framework tracks rate as well as enforcing a hard gate.

## 7. Model version pinning

The eval suite pins an **exact model version/snapshot**, never a moving
alias (e.g. "latest"), and records that version explicitly in every report
and trend-log row. Rationale: if the eval always calls through to whatever
model the deployment happens to be pointed at, a "regression" the suite
catches might actually be an upstream provider model change, not a code
change in AIAC — and historical trend data becomes uninterpretable if the
model silently changed underneath it. A deliberate model upgrade is its own
reviewed event: re-run the full suite against the new pinned version,
diff old-vs-new, then move the pin forward.

### 7.1 Initial model selection run

Before settling on the pinned model for routine use, run the **complete
suite once under two contrasting model tiers** — one strong (e.g. a
frontier model with the largest available context and highest reasoning
capability) and one weak (e.g. a smaller, cheaper model in the same
provider's line-up) — and compare their results head-to-head across every
quality attribute (Correctness, Robustness, Scale, Consistency). The
goal is twofold:

1. **Capability floor check** — establish how much model capability the
   pipeline actually needs. If the weak model already meets all gates, the
   strong model brings no measurable benefit; pinning the cheaper tier is
   the right call. If the weak model fails gates the strong model passes,
   the gap is evidence for the minimum capability required.
2. **Cost/quality trade-off data** — the comparison run produces concrete
   numbers (over-grant rate, under-grant rate, consistency disagreement
   rate, latency, estimated cost per run) for both tiers, making the
   model-selection decision reviewable and documented rather than intuitive.

This is a **manual, one-off run** (not recurring), performed before the
first pin is committed and whenever a candidate replacement model is being
evaluated. Its output is recorded in the structured trend log (§9) using a
`run_type = "model_selection"` tag so it is clearly distinguished from
routine regression runs.

## 8. Cadence

**Current state: everything is manual-only.** No suite is wired into CI to
gate PRs automatically.

The following tiering is recorded as a **recommendation for when automatic
gating is turned on** in the future — not the current behavior:

| Tier | Suites | Rationale |
|---|---|---|
| Every PR (gated) | Correctness (PRB-level, synthetic) + Robustness (PRB-level, synthetic) | Cheapest, fastest, most directly tied to "did this code change break policy interpretation" |
| Nightly (gated, non-blocking) | End-to-end Correctness + Robustness, Consistency, Scale-fixed-100 | Expensive enough that per-PR is wasteful; frequent enough to catch regressions within a day |
| Manual/on-demand only | Scale-exploratory, Keycloak-fidelity check | Diagnostic, not regression gates — no pass/fail semantics for the former, narrow fidelity-only purpose for the latter |

## 9. Reporting and trend persistence

Two artifacts, at different durability levels:

- **Detailed per-cell report** (gitignored, generated locally) — the
  existing legacy pattern (`reports/report_<timestamp>.md`) continues,
  extended to represent the new metric shapes (precision/recall breakdown,
  scale-dimension results, sensitivity/invariance split) rather than the
  legacy pass/fail-plus-`reasoning`-text shape alone.
- **Structured trend log** (**committed to git**) — a small append-only
  file (JSON or CSV) per suite, one row per run, holding model version,
  timestamp, and key metrics only (over-grant rate, under-grant rate,
  invariance rate, sensitivity rate, consistency disagreement rate,
  scale structural/correctness results) — not verbose per-cell reasoning
  text. Small enough to not bloat the repo; durable enough to actually plot
  drift over time across machines and contributors.

### 9.1 Actionable improvement feedback

The detailed per-cell report includes an **"Improvement recommendations"
section** generated after every run. The section surfaces findings as
concrete, targeted actions the team can act on — not just a statement of
which metrics failed. The following categories of recommendation are
produced whenever the supporting evidence is present:

| Finding type | Recommendation form |
|---|---|
| Scenario theme with elevated over-grant rate | Identify the specific policy clause or semantic pattern the PRB is over-interpreting; recommend a prompt constraint, a PRB graph edge, or a targeted scenario addition to the training/prompt corpus. |
| Scenario theme with elevated under-grant rate | Identify whether the miss is a parsing gap (policy text not recognized) or a reasoning gap (text parsed but grant not inferred); recommend either input normalization upstream of the PRB or an explicit reasoning step in the graph. |
| Sensitivity family failures (output did not change when it should have) | Flag which policy-text edit types the PRB is insensitive to (e.g. negation words, exception clauses, restriction words like "only"/"just"); recommend adding those edit patterns to the Robustness corpus and reviewing PRB prompts for those constructs. |
| Invariance family failures (output changed when it shouldn't have) | Flag which surface-form changes destabilize the PRB; recommend prompt hardening or normalization pre-processing. |
| Consistency disagreements | Note whether disagreements cluster on specific scenarios (structural prompt sensitivity) or appear random (temperature/batching noise); recommend `temperature=0` enforcement or a retry-with-majority-vote strategy accordingly. |
| Scale correctness degradation above a threshold | Identify whether degradation is in per-decision scale (large candidate lists) or total-corpus scale; recommend context-window management changes (chunking, summarization) or candidate-list pruning strategies respectively. |

Recommendations that have no supporting evidence in the current run are
omitted (not printed as vacuous "no issues found" items). Each
recommendation references the specific failing scenario(s) or metric
cell(s) that produced it, so the reader can verify the evidence directly
in the same report.

## 10. Framework trust

No mutation-testing validation of the harness itself (e.g. deliberately
injecting a known bug into the PRB and confirming the suite fails) is
included at this stage — the design (asymmetric gating, matched robustness
pairs, procedurally-generated-with-known-truth scale corpus) is trusted by
construction for now. Revisit if the suite is ever observed passing when it
shouldn't have.

## 11. Repository structure and migration

- The existing suite at `test/integration/eval/` is **moved** (not left in
  place, not rebuilt from scratch alongside it) to **`aiac/eval/`** — a
  top-level directory inside the `aiac/` package root, separate from
  `test/`.
- Everything already implemented there that covers something this spec
  calls for is **reused, adapted in place, rather than reimplemented**:
  - `conftest.py` — extended (not replaced) to natively support asymmetric
    precision/recall, scale-dimension results, and trend-log rows, on top
    of its existing `pytest_collection_modifyitems` /
    `pytest_runtest_logreport` / `pytest_sessionfinish` report-generation
    hooks.
  - `prb_direct.py` (`build_roles_and_scopes`) — the synthetic Role/Scope
    fixture builder, reused as the default PRB-level backend (§2.1).
  - `test_policy_pipeline_eval.py`'s Keycloak-provisioning and `opa eval`
    invocation path — reused for the end-to-end level and the Keycloak
    fidelity check.
  - `test_policy_pipeline_consistency.py` — reused as the basis for the
    Consistency suite (§6), adjusted to the trend-log requirement.
  - `test_policy_pipeline_robustness.py`, `scenarios/`,
    `scenarios_perturbed/` — reused as the basis for the Robustness suite
    (§4), extended with the sensitivity-family matched pairs this spec adds
    (the legacy suite only has the invariance family).
  - `probe_eval.rego`, `rego_out/` — reused as-is for end-to-end Rego
    output.
- New from this spec, not present in the legacy suite: the sensitivity
  (meaning-changing) robustness family; the Scale suite in its entirety
  (fixed-100 and exploratory tiers, both dimensions, procedurally generated
  ground truth); the Keycloak-fidelity periodic check as a distinct,
  narrowly-scoped suite; the committed structured trend log.

## 12. Open items for future revisit

- Under-grant tolerance threshold for the Correctness gate (currently
  "looser than zero-tolerance, exact value TBD").
- Whether to raise the Scale fixed-regression target above 100 services as
  confidence grows (documented production target is "hundreds").
- Whether to add mutation-testing validation of the harness (§10) if the
  suite is ever observed to pass when it shouldn't.
- Whether to test robustness/consistency across multiple model
  providers/versions, not just a single pinned version, as a separate axis.
- Minimum scenario count per correctness/robustness theme (deferred to
  implementation time).
