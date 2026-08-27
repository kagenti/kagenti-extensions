"""Lean proposer/auditor message builders for both PRB directions.

No worked examples, no scenario domain content. The static system message carries
the task framing plus two shared safety meta-rules (deny-by-default / policy-silence,
and stay strictly scoped to the single focal entity) and two shared mapping rules
(capability projection and relationship scoping — see _MAPPING_RULES). Both the
proposer and the auditor reason under the same rules because both make the same
grant decision. Everything variable — policy text, focal entity, candidates, and
any auditor feedback — goes in the user message so it is observable in tests.

The user message's POLICY block is built from three layers, in order: the
least-privilege deny-by-default directive (``_GRANT_ACCESS`` here in the prompt),
the bundled generic baseline policy (``generic_policy.md`` — agent operator-role
domain confinement, applies to every policy decision), and finally the scenario policy
text. Factoring the universal clauses out of the file means a scenario policy file
only has to state what is specific to that scenario; the baseline is always present.
"""

from pathlib import Path

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

# Least-privilege framing that heads every POLICY block, before any file-based policy.
_GRANT_ACCESS = "Grant access on a least-privilege basis: allow only what this policy states; deny by default."

# Generic baseline policy, bundled next to this module and prepended to every scenario policy.
# Loaded once at import (static, domain-agnostic — safe to read eagerly).
_GENERIC_POLICY = (Path(__file__).parent / "generic_policy.md").read_text(encoding="utf-8").strip()


def _policy_block(policy_text: str) -> str:
    """Compose the POLICY block in three labeled layers: the least-privilege directive, then the
    generic baseline (explicitly grants-only — never a source of denials), then the scenario policy.
    The labels let the deny/exclusivity rules bind to the SCENARIO layer only."""
    return (
        f"{_GRANT_ACCESS}\n\n"
        f"BASELINE POLICY (grants only — never a source of denials):\n{_GENERIC_POLICY}\n\n"
        f"SCENARIO POLICY:\n{policy_text}"
    )


_SAFETY = (
    "Rules:\n"
    "1) Deny by default: grant a pair only when it is supported by evidence about the focal entity "
    "itself — either the provided policy, OR the focal entity's and the candidate's own descriptions "
    "establishing that the candidate performs an operation the scope covers (see rule 3). Policy "
    "silence is not by itself a reason to deny a pair the descriptions already establish, nor a "
    "license to grant one they do not; when neither the policy nor those descriptions support the "
    "pair, do not grant. Silence is a silent non-grant (no rule at all) — NOT an explicit "
    "prohibition (see rule 5). A statement about any OTHER entity is never support (see rule 4).\n"
    "2) Stay strictly scoped to the single focal entity described below; ignore anything else. A "
    "grant requires the focal entity and the candidate to operate in the SAME domain: never pair "
    "across domains — an issues-domain role/scope with a source-domain scope/role, or vice versa — "
    "and a candidate whose domain does not match the focal entity's, or that the policy never "
    "connects to it, earns nothing. (Cross-GRANULARITY within one domain — a fine operation earning "
    "the coarse capability that covers it — is fine and is rule 3; cross-DOMAIN never is.)"
)

# Shared mapping rules appended to BOTH system messages so the proposer and the auditor decide grants
# under the same reasoning (a proposer-only or auditor-only rule lets the two sides diverge).
#
# Rule 3 (capability projection): a scope/capability names a SET of operations; any one covered
#   operation established for a candidate — by the policy OR by the focal/candidate descriptions —
#   grants the whole scope (read-only still counts). Without this, a candidate with partial access to
#   a capability's domain is wrongly dropped — e.g. a read-only "consults issues" subject failing to
#   earn the issue-management agent scope.
# Rule 4 (relationship scoping): a policy may state several distinct access relationships over the
#   same entities. Each grant is judged by what the policy/descriptions establish for THAT candidate
#   in relation to the focal entity; a statement about an entity that is NEITHER the focal entity NOR
#   a candidate describes a different relationship and is never evidence. The "neither focal nor
#   candidate" scoping matters in BOTH directions: focal=scope/candidates=roles (mapping a/b) and
#   focal=role/candidates=scopes (mapping c) — in the latter the focal role's OWN description must
#   still count, so it must not be treated as a non-candidate to ignore. Without this a scope that
#   appears in two relationships bleeds across them — wrongly rejecting a single-subject grant, or
#   inventing an agent-role grant from an unrelated subject statement.
_MAPPING_RULES = (
    "\n3) A scope or capability names a set of operations (see its description). Grant it to a "
    "candidate when the policy — or the focal entity's and the candidate's own descriptions — shows "
    "that candidate performs ANY operation the scope covers. Projection is UPWARD only: a shown "
    "operation earns a COARSER capability scope that already INCLUDES that operation — a candidate "
    "shown to read issues earns an issue-management capability (which covers reading). It NEVER "
    "crosses to a SIBLING operation the candidate is not shown to perform: read access alone earns "
    "no write scope (issues-read does NOT imply issues-write), and write access earns no read-only "
    "scope. Grant each fine-grained scope strictly on the operation it names. A candidate shown to "
    "perform no covered operation is simply not granted (rule 1); that is a non-grant, not a "
    "prohibition.\n"
    "4) A policy may describe several different access relationships over the same entities. Judge "
    "each candidate independently, by what the policy or the descriptions establish for THAT candidate "
    "in relation to the focal entity. Base each grant only on evidence about that specific candidate "
    "and the focal entity; a statement about any OTHER entity — even one sharing the same domain or "
    "theme (e.g. a differently-named role or subject with related access) — concerns a different "
    "relationship and is never evidence for or against the grant, even when it names the focal entity "
    "or the scope. ONE SANCTIONED EXCEPTION: exclusive/restrictive scoping ABOUT THE FOCAL ENTITY "
    "(rule 6) is legitimate evidence to deny the complement — that is the only cross-candidate "
    "inference allowed."
)

# Deny / exclusivity contract — appended to BOTH the proposer and auditor system messages so the
# two halves of the LLM contract cannot diverge. Deny extraction is SCENARIO-only; the baseline
# is grants-only.
_DENY_RULES = (
    "\nThe remaining rules concern PROHIBITIONS and apply to the SCENARIO policy ONLY. If the "
    "scenario policy contains no prohibitive language (rule 5) and no exclusivity wording (rule 6), "
    "return EMPTY denied lists and exclusivity=false — a purely permissive policy prohibits nothing; "
    "never invent a prohibition to hedge.\n"
    "5) EXPLICIT PROHIBITIONS -> deny. Prohibitive language in the SCENARIO policy about a "
    "specific pair — 'must not', 'cannot', 'may not', 'is forbidden', 'never', 'except', 'but not', "
    "'read-only' / 'may read but not write' — records that candidate as a PROHIBITION (a durable "
    "DENY), not merely a non-grant. This applies to the scenario policy ONLY: the baseline policy is "
    "grants-only and is NEVER a source of prohibitions. Silence about a pair, and a plain "
    "non-exclusive grant, impose NOTHING on anything else — they never deny.\n"
    "6) EXCLUSIVITY ('only'). Restrictive/exclusive language about the FOCAL entity — 'only', 'solely', "
    "'exclusively', 'nothing else' — means the focal entity's access is closed to EXACTLY the granted "
    "set. Signal this by setting the exclusivity flag true; do NOT enumerate the other candidates "
    "yourself (the builder derives the complete complement from the candidate set). A non-exclusive "
    "grant leaves the flag false and denies nothing.\n"
    "7) The grant list and the prohibition list are MUTUALLY EXCLUSIVE, except when the scenario "
    "policy genuinely establishes BOTH a grant and a prohibition for the same candidate (a direct "
    "conflict, or a coarse scope partly permitted and partly forbidden) — then, and only then, list "
    "that candidate in both. That overlap is the contradiction signal; never invent it to hedge."
)

_PROPOSER_SYSTEM = (
    "You map an access policy to concrete GRANTS. Your primary task is to select the granted "
    "candidates for the focal entity under least-privilege. Only when the scenario policy explicitly "
    "prohibits or restricts access do you also report the prohibited candidates and whether access "
    "is exclusive; for a purely permissive policy those are empty.\n"
    + _SAFETY
    + _MAPPING_RULES
    + _DENY_RULES
)
_AUDITOR_SYSTEM = (
    "You audit a proposed set of grants and prohibitions. Approve only if every granted pair is "
    "policy-supported — REJECT any grant unsupported by the policy or the descriptions, any grant in "
    "a domain the candidate is not shown to act in, and any grant for a candidate the policy never "
    "mentions. Every prohibited pair must be a genuine explicit-prohibition or exclusivity deny, the "
    "exclusivity flag must be truly asserted by the SCENARIO policy, and for a purely permissive "
    "policy both denied lists must be empty. When a candidate is named in BOTH lists (a conflict), "
    "adjudicate it: a genuine grant-and-prohibit collision is a contradiction (report it), a mere "
    "proposer slip is an ordinary rejection.\n" + _SAFETY + _MAPPING_RULES + _DENY_RULES
)


def build_proposer_messages(
    policy_text: str,
    focal: str,
    candidates: str,
    contract: str,
    audit_feedback: str | None,
    *,
    direction: str,
) -> list[BaseMessage]:
    # ``direction`` leads the body so the gate axis (what the focal is, what the candidates are, and
    # that entities named only in the policy prose are NOT candidates) frames how the policy is read
    # — without it a focal whose name echoes a policy domain drags the model onto the wrong axis.
    body = (
        f"{direction}\n\nPOLICY:\n{_policy_block(policy_text)}\n\n"
        f"FOCAL ENTITY:\n{focal}\n\nCANDIDATES:\n{candidates}\n\n{contract}"
    )
    if audit_feedback:
        body += f"\n\nA prior proposal was REJECTED. Fix per this feedback:\n{audit_feedback}"
    return [SystemMessage(content=_PROPOSER_SYSTEM), HumanMessage(content=body)]


def build_auditor_messages(
    policy_text: str,
    focal: str,
    candidates: str,
    selected_names: list[str],
    denied_names: list[str],
    conflict_names: list[str],
    *,
    direction: str,
) -> list[BaseMessage]:
    # Same ``direction`` framing as the proposer: the auditor previously got NO axis hint (the
    # proposer alone received the contract), which let it adjudicate against the wrong candidate set.
    body = (
        f"{direction}\n\nPOLICY:\n{_policy_block(policy_text)}\n\n"
        f"FOCAL ENTITY:\n{focal}\n\nCANDIDATES:\n{candidates}\n\n"
        f"PROPOSED GRANTS (names): {selected_names}\n"
        f"PROPOSED PROHIBITIONS (names): {denied_names}"
    )
    if conflict_names:
        body += (
            f"\n\nCONFLICT (named in BOTH lists): {conflict_names}. For each, decide whether the "
            "policy GENUINELY both grants and prohibits it -- a direct conflict, or a coarse scope "
            "partly permitted and partly forbidden -- versus a mere proposer error. Report genuine "
            "ones in `contradictions` (name the kind in each description); if it is just a proposer "
            "mistake, leave `contradictions` empty and reject with a reason so it can re-propose."
        )
    return [SystemMessage(content=_AUDITOR_SYSTEM), HumanMessage(content=body)]
