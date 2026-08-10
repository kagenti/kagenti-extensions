"""Unit tests for aiac.agent.shared.roles.flatten_role — pure function, no mocks.

``flatten_role(role)`` returns the closure of a role: the role itself plus all
descendants (recursively via ``role.childRoles``), de-duplicated by ``role.id``.
"""

from unittest.mock import patch

from aiac.agent.shared.roles import flatten_role
from aiac.idp.configuration.models import Role


def _role(id: str, name: str = "", *, composite: bool = False, children=None) -> Role:
    return Role(
        id=id,
        name=name or id,
        composite=composite,
        childRoles=children or [],
    )


def test_non_composite_role_yields_itself_only():
    role = _role("r-leaf", composite=False)

    assert flatten_role(role) == [role]


def test_composite_with_two_children_yields_self_plus_descendants():
    child1 = _role("r-c1")
    child2 = _role("r-c2")
    parent = _role("r-parent", composite=True, children=[child1, child2])

    assert flatten_role(parent) == [parent, child1, child2]


def test_nested_composite_returns_full_recursive_closure():
    grandchild = _role("r-gc")
    child = _role("r-c", composite=True, children=[grandchild])
    parent = _role("r-parent", composite=True, children=[child])

    assert flatten_role(parent) == [parent, child, grandchild]


def test_diamond_shared_child_is_deduplicated_by_id():
    shared = _role("r-shared")
    branch_a = _role("r-a", composite=True, children=[shared])
    branch_b = _role("r-b", composite=True, children=[shared])
    root = _role("r-root", composite=True, children=[branch_a, branch_b])

    result = flatten_role(root)

    # The shared child is reachable via two parents but appears exactly once.
    assert [r.id for r in result] == ["r-root", "r-a", "r-shared", "r-b"]
    assert sum(1 for r in result if r.id == "r-shared") == 1


def test_flatten_role_makes_no_idp_config_call():
    child = _role("r-c")
    parent = _role("r-parent", composite=True, children=[child])

    # If flatten_role ever reached for the IdP, this patched Configuration would
    # register a call. It operates purely on the in-memory childRoles graph.
    with patch("aiac.idp.configuration.api.Configuration") as config:
        result = flatten_role(parent)

    assert result == [parent, child]
    config.assert_not_called()
    config.for_realm.assert_not_called()
