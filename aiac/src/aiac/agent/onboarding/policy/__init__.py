"""
AIAC Policy Agent - Access Control Policy Builder Package

AI-powered access control policy builder using LangGraph workflows and LLMs.
Converts natural language policy descriptions into structured YAML policies for Keycloak.

Main Components:
    - PolicyBuilder: Full policy generation for all services in a realm
    - ServicePolicyBuilder: Service-scoped policy generation
    - SingleRoleMapper: Individual role mapping with semantic analysis

Quick Start:
    >>> from pathlib import Path
    >>> from full_policy_agent import PolicyBuilder
    >>>
    >>> builder = PolicyBuilder(realm="my-realm", config_path=Path("config.yaml"))
    >>> result = builder.generate_policy("Admins have full access to all services")
    >>>
    >>> if result["success"]:
    ...     builder.save_policy(result["yaml_output"], "policy.yaml")

For detailed documentation, see README.md in this directory.
"""

from full_policy_agent.graph import PolicyBuilder
from service_policy_agent.graph import ServicePolicyBuilder
from single_role_agent.graph import SingleRoleMapper

__version__ = "1.0.0"
__author__ = "AIAC Development Team"
__license__ = "MIT"

__all__ = [
    "PolicyBuilder",
    "ServicePolicyBuilder",
    "SingleRoleMapper",
]

