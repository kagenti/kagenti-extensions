#!/usr/bin/env python3
"""
Single Privilege Mapper Package

This package provides functionality for mapping individual privileges
to real roles (realm roles) that should have access to them.
"""

from .graph import SinglePrivilegeMapper, SinglePrivilegeMapperConfig
from .state import SinglePrivilegeState

__all__ = [
    'SinglePrivilegeMapper',
    'SinglePrivilegeMapperConfig',
    'SinglePrivilegeState',
]

# Made with Bob