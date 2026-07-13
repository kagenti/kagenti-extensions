"""
Single Privilege Mapper

Maps a single privilege to the realm roles that should have access to it.
"""

from .graph import SinglePrivilegeMapper
from .state import SinglePrivilegeState

__all__ = [
    "SinglePrivilegeMapper",
    "SinglePrivilegeState",
]