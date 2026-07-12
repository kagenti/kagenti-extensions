"""Service Onboarding Orchestrator (UC1).

The only use case with an Orchestrator, because it is a **two-stage** pipeline. Invoked by
the Controller for the ``aiac.apply.service.{id}`` / ``POST /apply/service/{service_id}``
trigger, it sequences the two sub-agents and returns ``(list[PolicyRule], override=False)``:

    1. Service Provision  — classifies the service and writes its roles/scopes into the IdP,
       producing the discovered ``service_type``.
    2. Service Policy Builder — reads the excluded-self IdP universe and builds the rules.

There is **no Apply stage** here: the Controller makes the single
``compute_and_apply(rules, override=False)`` (PCE) call. UC1 is incremental, so ``override``
is always ``False`` (append; existing roles keep their other access).

**Replay safety (at-least-once delivery):** Provision IdP writes are idempotent and the PCE
reconcile is idempotent, so a crash between stages simply re-runs the full pipeline to
convergence on NATS redelivery. No rollback logic.
"""

from aiac.agent.uc.onboarding.policy_builder.builder import ServicePolicyBuilder
from aiac.agent.uc.onboarding.provision.graph import build_provision_graph
from aiac.agent.uc.onboarding.provision.state import OnboardingProvisionState, Trigger
from aiac.policy.model.models import PolicyRule


def onboard_service(service_id: str) -> tuple[list[PolicyRule], bool]:
    provision = build_provision_graph().invoke(
        OnboardingProvisionState(trigger=Trigger(entity_id=service_id))
    )
    service_type = provision["service_type"]
    rules = ServicePolicyBuilder.build(service_id, service_type)
    return rules, False
