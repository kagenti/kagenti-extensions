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
    """Compose the POLICY block: least-privilege directive, then the generic baseline, then the
    scenario policy."""
    return f"{_GRANT_ACCESS}\n\n{_GENERIC_POLICY}\n\n{policy_text}"

_SAFETY = (
    "Rules:\n"
    "1) Deny by default: grant a pair only when it is supported by evidence about the focal entity "
    "itself — either the provided policy, OR the focal entity's and the candidate's own descriptions "
    "establishing that the candidate performs an operation the scope covers (see rule 3). Policy "
    "silence is not by itself a reason to deny a pair the descriptions already establish, nor a "
    "license to grant one they do not; when neither the policy nor those descriptions support the "
    "pair, do not grant. A statement about any OTHER entity is never support (see rule 4).\n"
    "2) Stay strictly scoped to the single focal entity described below; ignore anything else."
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
    "that candidate performs ANY operation the scope covers; partial access (e.g. read-only) still "
    "grants the scope. A candidate shown to perform no covered operation is denied (rule 1).\n"
    "4) A policy may describe several different access relationships over the same entities. Judge "
    "each candidate independently, by what the policy or the descriptions establish for THAT candidate "
    "in relation to the focal entity. Base each grant only on evidence about that specific candidate "
    "and the focal entity; a statement about any OTHER entity — even one sharing the same domain or "
    "theme (e.g. a differently-named role or subject with related access) — concerns a different "
    "relationship and is never evidence for or against the grant, even when it names the focal entity "
    "or the scope."
)

_PROPOSER_SYSTEM = "You map access policy to concrete grants.\n" + _SAFETY + _MAPPING_RULES
_AUDITOR_SYSTEM = (
    "You audit a proposed set of grants. Approve only if every granted pair is "
    "policy-supported and nothing unsupported slipped in.\n" + _SAFETY + _MAPPING_RULES
)


def build_proposer_messages(
    policy_text: str,
    focal: str,
    candidates: str,
    contract: str,
    audit_feedback: str | None,
) -> list[BaseMessage]:
    body = f"POLICY:\n{_policy_block(policy_text)}\n\nFOCAL ENTITY:\n{focal}\n\nCANDIDATES:\n{candidates}\n\n{contract}"
    if audit_feedback:
        body += f"\n\nA prior proposal was REJECTED. Fix per this feedback:\n{audit_feedback}"
    return [SystemMessage(content=_PROPOSER_SYSTEM), HumanMessage(content=body)]


def build_auditor_messages(
    policy_text: str,
    focal: str,
    candidates: str,
    selected_names: list[str],
) -> list[BaseMessage]:
    body = (
        f"POLICY:\n{_policy_block(policy_text)}\n\nFOCAL ENTITY:\n{focal}\n\nCANDIDATES:\n{candidates}\n\n"
        f"PROPOSED SELECTION (names): {selected_names}"
    )
    return [SystemMessage(content=_AUDITOR_SYSTEM), HumanMessage(content=body)]
