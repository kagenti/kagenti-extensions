"""Lean proposer/auditor message builders for both PRB directions.

No worked examples, no domain content. The static system message carries the
task framing plus two shared safety meta-rules (deny-by-default / policy-silence,
and stay strictly scoped to the single focal entity). The auditor carries one
additional relationship-scoping rule: a policy statement about an entity that is
not among the candidates describes a different access relationship and is not
evidence for or against a candidate's grant (see _AUDITOR_DIMENSION). Everything
variable — policy text, focal entity, candidates, and any auditor feedback — goes
in the user message so it is observable in tests.
"""

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

_SAFETY = (
    "Rules:\n"
    "1) Deny by default: grant a pair only if the provided policy explicitly supports it; "
    "if the policy is silent, do not grant.\n"
    "2) Stay strictly scoped to the single focal entity described below; ignore anything else."
)
_PROPOSER_SYSTEM = "You map access policy to concrete grants.\n" + _SAFETY

# Auditor-only meta-rule. A policy may state several distinct access relationships over the same
# entities (e.g. which subjects may reach a capability, and which operations a capability bundles).
# The auditor judges ONE relationship at a time — whether each listed candidate is granted the focal
# entity — so a statement about a NON-candidate entity describes a different relationship and must not
# count against a candidate's otherwise-supported grant. Without this, a scope granted to a single
# subject that ALSO appears on the other side of a different relation can be wrongly rejected.
_AUDITOR_DIMENSION = (
    "\n3) A policy may describe several different access relationships over the same entities. Judge "
    "each candidate independently, by what the policy grants THAT candidate in relation to the focal "
    "entity — an explicit statement or an unambiguous description. A policy statement about an entity "
    "that is NOT among the listed candidates concerns a different relationship: it is never evidence "
    "for or against a candidate's grant, even when it names the focal entity."
)
_AUDITOR_SYSTEM = (
    "You audit a proposed set of grants. Approve only if every granted pair is "
    "policy-supported and nothing unsupported slipped in.\n" + _SAFETY + _AUDITOR_DIMENSION
)


def build_proposer_messages(
    policy_text: str,
    focal: str,
    candidates: str,
    contract: str,
    audit_feedback: str | None,
) -> list[BaseMessage]:
    body = f"POLICY:\n{policy_text}\n\nFOCAL ENTITY:\n{focal}\n\nCANDIDATES:\n{candidates}\n\n{contract}"
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
        f"POLICY:\n{policy_text}\n\nFOCAL ENTITY:\n{focal}\n\nCANDIDATES:\n{candidates}\n\n"
        f"PROPOSED SELECTION (names): {selected_names}"
    )
    return [SystemMessage(content=_AUDITOR_SYSTEM), HumanMessage(content=body)]
