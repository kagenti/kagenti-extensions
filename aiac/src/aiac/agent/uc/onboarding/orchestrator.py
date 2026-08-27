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
from aiac.policy.model.models import PolicyRule, RuleEffect


def onboard_service(
    service_id: str, default_effect: RuleEffect = RuleEffect.DENY
) -> tuple[list[PolicyRule], bool, RuleEffect]:
    """Sequence Provision → Policy Builder and return ``(rules, override=False, default_effect)``.

    ``default_effect`` is passed straight back to the Controller so it reaches the single
    ``compute_and_apply`` call and lands on every derived ``AgentPolicyModel``. It defaults to
    ``DENY`` (least-privilege); a caller onboarding a service that should default to ``ALLOW``
    supplies it here. This is the caller-facing surface for requesting a permissive default
    end-to-end (onboard → PCE → derived APM → OPA)."""
    provision = build_provision_graph().invoke(
        OnboardingProvisionState(trigger=Trigger(entity_id=service_id))
    )
    service_type = provision["service_type"]
    rules = ServicePolicyBuilder.build(service_id, service_type)
    return rules, False, default_effect
