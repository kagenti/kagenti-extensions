# AIAC

The AIAC agent turns natural-language authorization policy into applied
PolicyRules. It surveys IdP roles and scopes, drives an LLM-backed Policy Rules
Builder over focal entities, and computes/applies the resulting rules through a
two-layer policy stack. This glossary fixes the vocabulary for how the builder
grants, prohibits, and reports collisions.

## Language

**Focal entity**:
The single role or scope a Policy Rules Builder pass is centred on for one
`build()` run. Every pass fans candidates against exactly one focal per run.
_Avoid_: subject, principal, target.

**Scope-focal pass**:
The pass centred on a scope, fanning candidate roles over it. It is the sole
**grant authority** for that scope.
_Avoid_: scope pass, forward pass.

**User-role-focal pass** (a.k.a. **Door B**):
A pass centred on a `kind=User` role, fanning it over the focus service's own
scopes to emit the **deny** rules that a user's exclusivity ("Testers may access
**only** issues") implies — prohibitions the scope-focal pass structurally
cannot express. Contributes denies only; never broadens access.
_Avoid_: role pass (ambiguous with the agent-role-focal pass), Door B pass.

**Grant authority**:
The property that grants on a given scope come from exactly one place — the
scope-focal pass. Door B adds only prohibitions and never grants.
_Avoid_: owner, source of truth.

**Scope-exclusivity**:
Exclusive/restrictive language centred on a **scope** ("Only developers may read
source") — it closes that scope to a grantee role set. Owned by the
**scope-focal pass** (the subject gate): it grants the named roles and denies the
complement of candidate roles. Distinct from **role-exclusivity**.
_Avoid_: treating it as a Door B (role-focal) deny trigger.

**Role-exclusivity**:
Exclusive/restrictive language centred on a user **role** ("Testers may access
**only** issues") — it closes that role to a scope set. Owned by **Door B** (the
user-role-focal pass): it denies the complement of candidate scopes. Conflating
it with **scope-exclusivity** — treating a scope-exclusive statement about a
*different* role as grounds for a Door B deny on the focal role — is the category
error #2511 corrects: that complement belongs to the scope-focal pass, so an
absent Door B prohibition is correct, not a defect.
_Avoid_: role pass exclusivity, subject exclusivity.

**Contradiction**:
An *intra-pass* grant∩deny: one focal's own proposed rule set both grants and
prohibits the same candidate. Detected by the LLM auditor within a single pass,
which fails that pass closed. Modelled by `Contradiction` / raised as
`PolicyContradictionError`.
_Avoid_: using "conflict" for this — the two are distinct.

**Conflict**:
A *cross-pass* grant∩deny: an `Allow` from one pass and a `Deny` from another on
the **same `(role, scope)`** pair. Structural (a pure id-level allow∩deny
set-intersection over the assembled rules), not LLM-audited. Modelled by
`Conflict` / `ConflictReport`.
_Avoid_: using "contradiction" for this.

**Within-batch conflict**:
A **conflict** whose two rules are produced in one `build()` call — i.e. one
`/apply` request. This is the Door B case: at the focus service's own-scope
onboarding, both the scope-focal grant and the Door B deny (and any collision
between them) are in hand in the same build. In scope.
_Avoid_: intra-request conflict.

**Cross-run conflict** (a.k.a. **cross-service conflict**):
A **conflict** whose two rules are produced in separate onboarding requests and
collide only in the persisted SPM store. Surfaced at `/apply` by the
cross-service check, which reads the already-applied rules of the services that
own the touched scopes and folds them into detection; the pure within-build
structural pass alone does not see it.
_Avoid_: cross-request conflict, store conflict.

**Identify-never-reconcile**:
The governing principle: a `(role, scope)` carrying both an `Allow` and a `Deny`
**is** a conflict — surface it, never resolve it. No precedence, no
"deny wins," no merge. See `docs/adr/0001-identify-never-reconcile.md`.
_Avoid_: deny-overrides, conflict resolution.
