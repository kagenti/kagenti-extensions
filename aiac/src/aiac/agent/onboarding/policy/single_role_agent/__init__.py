#!/usr/bin/env python3
"""
Single Role Agent Package

This package provides functionality for mapping individual service roles
to real roles (realm roles) that should have access to them.
"""

from .graph import SingleRoleMapper, SingleRoleMapperConfig
from .state import SingleRoleState

__all__ = [
    'SingleRoleMapper',
    'SingleRoleMapperConfig',
    'SingleRoleState',
]

# Made with Bob