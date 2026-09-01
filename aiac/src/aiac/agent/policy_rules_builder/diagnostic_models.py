"""Structured models for the read-only policy **conflict-check** diagnostic (feature #154).

This module defines ONLY the stable serialization shape shared by the diagnostic engine, the
survey use-case, and the ``POST /policy/check`` route — no pipeline logic. The live ``/apply``
path and its models (``PolicyRule``, ``Contradiction``, ``AuditVerdict`` in ``graph.py`` /
``policy.model.models``) are deliberately untouched (decision D11): the typed conflict ``kind``
lives here, produced by the diagnostic's ``explain`` node, rather than being bolted onto the
shared ``Contradiction`` / ``AuditVerdict`` that the byte-for-byte-stable live path depends on.

Style mirrors the rest of the agent/policy layer: ``str``-subclass ``Enum``s (so
``FocalType.ROLE == "role"`` holds and they serialize as their string value) and pydantic
``BaseModel`` with ``ConfigDict(extra="ignore")``.
"""

from enum import Enum

from pydantic import BaseModel, ConfigDict


class FocalType(str, Enum):
    """Which axis the focal entity sits on — i.e. which side drove the survey run. A
    ``str`` enum, so ``FocalType.ROLE == "role"`` holds and it serializes as ``"role"`` /
    ``"scope"``."""

    ROLE = "role"
    SCOPE = "scope"


class ConflictKind(str, Enum):
    """The two recognized contradiction kinds (D6). ``direct`` = the same (role, scope) pair is
    both granted and prohibited; ``coarse_scope`` = a coarse capability is granted while a finer
    one it subsumes is prohibited (a granularity mismatch). Classified by the ``explain`` node
    (D11), not read from the auditor. A ``str`` enum, so ``ConflictKind.DIRECT == "direct"``."""

    DIRECT = "direct"
    COARSE_SCOPE = "coarse_scope"


class ConflictStatus(str, Enum):
    """The report's top-level outcome. EXACTLY three values (the precedence that selects among
    them is documented on :meth:`ConflictReport.derive_status`). ``no_conflict`` is a *positive*
    clean result and is only ever reached when ≥1 entity was evaluated with nothing outstanding —
    never a stand-in for an incomplete/failed run."""

    NO_CONFLICT = "no_conflict"
    CONFLICTS_FOUND = "conflicts_found"
    INCOMPLETE = "incomplete"


class UnevaluatedReason(str, Enum):
    """Why a focal entity could not be evaluated. Currently a single value: ``nonconvergence``
    (the entity exhausted the audit retry budget without a verdict). Kept as an enum so callers
    switch on a stable token, with the free-text ``detail`` carrying specifics."""

    NONCONVERGENCE = "nonconvergence"


class EntityRef(BaseModel):
    """Minimal ``{name, id}`` reference to a resolved IdP entity (role or scope). The ``id`` is
    the Keycloak entity id re-joined from the audit output's name string (D12)."""

    model_config = ConfigDict(extra="ignore")

    name: str
    id: str


class FocalRef(EntityRef):
    """A ``{name, id, type}`` reference to the *focal* entity of a run — an :class:`EntityRef`
    plus the axis (``type``) it sits on, which tells the reader which side drove the run."""

    type: FocalType


class Conflict(BaseModel):
    """One genuine, auditor-confirmed grant/prohibit contradiction for a single (role, scope)
    pair. ``focal`` records which side drove the run; ``role`` / ``scope`` are the two colliding
    entities regardless of direction. Quotes are verbatim substrings of the candidate policy text
    (D5); when substring validation fails, the conflict is kept, ``quotes_verified`` is ``False``,
    and the explanation falls back to the auditor description."""

    model_config = ConfigDict(extra="ignore")

    focal: FocalRef
    role: EntityRef
    scope: EntityRef
    kind: ConflictKind
    granting_quotes: list[str] = []
    prohibiting_quotes: list[str] = []
    explanation: str
    quotes_verified: bool


class Unevaluated(BaseModel):
    """A focal entity the survey could not evaluate (e.g. retry-budget exhaustion). Listed so a
    partial run is never mistaken for a clean one — its presence forces status away from
    ``no_conflict``."""

    model_config = ConfigDict(extra="ignore")

    focal: FocalRef
    reason: UnevaluatedReason = UnevaluatedReason.NONCONVERGENCE
    detail: str | None = None


class ConflictReport(BaseModel):
    """The single object the diagnostic returns: every conflict found across the surveyed
    focal entities, every entity that could not be evaluated, and the derived ``status``."""

    model_config = ConfigDict(extra="ignore")

    conflicts: list[Conflict] = []
    unevaluated: list[Unevaluated] = []
    status: ConflictStatus

    @staticmethod
    def derive_status(
        conflicts: list[Conflict],
        unevaluated: list[Unevaluated],
        evaluated_count: int,
    ) -> ConflictStatus:
        """Apply the authoritative status precedence (spec §"Status enum + precedence"):

        ``conflicts_found`` ⇔ ``conflicts`` non-empty; else ``incomplete`` ⇔ ``evaluated_count
        == 0`` OR ``unevaluated`` non-empty; else ``no_conflict``. Both ``incomplete`` disjuncts
        are load-bearing: ``evaluated_count == 0`` catches the empty-input / zero-focal case,
        ``unevaluated != []`` catches retry-exhaustion on some entities while the rest are clean.
        ``no_conflict`` is reached only when ≥1 entity was evaluated AND ``conflicts == []`` AND
        ``unevaluated == []`` — so an outage never looks identical to a clean policy."""
        if conflicts:
            return ConflictStatus.CONFLICTS_FOUND
        if evaluated_count == 0 or unevaluated:
            return ConflictStatus.INCOMPLETE
        return ConflictStatus.NO_CONFLICT

    @classmethod
    def from_survey(
        cls,
        conflicts: list[Conflict],
        unevaluated: list[Unevaluated],
        evaluated_count: int,
    ) -> "ConflictReport":
        """Assemble a report, deriving ``status`` from the survey outcome via
        :meth:`derive_status`. The survey use-case (out of scope for the model layer) owns *what*
        counts as an evaluated entity; this only encodes the precedence."""
        return cls(
            conflicts=conflicts,
            unevaluated=unevaluated,
            status=cls.derive_status(conflicts, unevaluated, evaluated_count),
        )
