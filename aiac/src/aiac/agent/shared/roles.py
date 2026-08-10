"""Shared agent helpers for working with IdP roles."""

from aiac.idp.configuration.models import Role


def flatten_role(role: Role) -> list[Role]:
    """Closure of a role: the role itself plus all descendants.

    Recurses via ``role.childRoles``, de-duplicated by ``role.id``. ``Role`` is
    not hashable, so seen ids are tracked rather than adding ``Role`` objects to
    a set. A non-composite role yields ``[role]``.

    Pure function — no IdP call. ``role.childRoles`` is already populated on the
    ``Role`` objects read from ``aiac.idp.configuration``.
    """
    closure: list[Role] = []
    seen: set[str] = set()

    def _visit(current: Role) -> None:
        if current.id in seen:
            return
        seen.add(current.id)
        closure.append(current)
        for child in current.childRoles:
            _visit(child)

    _visit(role)
    return closure
