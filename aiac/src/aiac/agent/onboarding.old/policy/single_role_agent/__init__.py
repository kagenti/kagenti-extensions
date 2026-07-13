"""
Single Role Scope Mapper

Maps a single realm role to the privileges/scopes it should hold.
"""

from .graph import SingleRoleMapper
from .state import SingleRoleState

__all__ = [
    "SingleRoleMapper",
    "SingleRoleState",
]
