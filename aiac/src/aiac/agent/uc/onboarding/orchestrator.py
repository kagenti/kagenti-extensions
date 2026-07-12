"""Service Onboarding Orchestrator (UC1) — stub.

Two-stage (Provision → Service Policy Builder) orchestration lands in 3.4/3.5/3.6.
At the foundation stage it returns an empty rule set with ``override=False``
(service onboarding merges additively).
"""

from aiac.policy.model.models import PolicyRule


def onboard_service(service_id: str) -> tuple[list[PolicyRule], bool]:
    return [], False
