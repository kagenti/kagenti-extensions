"""Service Onboarding Orchestrator (UC1).

The only use case with an Orchestrator, because it is a **two-stage** pipeline. Invoked by
the Controller for the ``aiac.apply.service.{id}`` / ``POST /apply/service/{service_id}``
trigger, it sequences the two sub-agents and returns ``(list[PolicyRule], override=False)``:

    1. Service Provision  — classifies the service and writes its roles/scopes into the IdP,
       producing the discovered ``service_type`` and the **created-manifest** (exactly the
       roles/scopes it created on this run — see ``provision_service``).
    2. Service Policy Builder — reads the excluded-self IdP universe and builds the rules.

There is **no Apply stage** here: the Controller makes the single
``compute_and_apply(rules, override=False)`` (PCE) call. UC1 is incremental, so ``override``
is always ``False`` (append; existing roles keep their other access).

**Replay safety (at-least-once delivery):** Provision IdP writes are idempotent and the PCE
reconcile is idempotent, so a crash between stages simply re-runs the full pipeline to
convergence on NATS redelivery. A build **failure**, however, triggers a **compensating
rollback** (UC1-only) before the error propagates — see :func:`_rollback`.
"""

import logging

from aiac.agent.policy_rules_builder.conflict_detection import PolicyConflictError
from aiac.agent.policy_rules_builder.graph import (
    LLMAccessError,
    PolicyRulesBuilderError,
    UnparseableLLMResponseError,
)
from aiac.agent.uc.onboarding.policy_builder.builder import ServicePolicyBuilder
from aiac.agent.uc.onboarding.provision.graph import build_provision_graph
from aiac.agent.uc.onboarding.provision.state import OnboardingProvisionState, Trigger
from aiac.idp.configuration.api import Configuration
from aiac.policy.model.models import PolicyRule, RuleEffect

logger = logging.getLogger(__name__)

# The build failures that trigger the UC1 compensating rollback. A conflict finding
# (``PolicyConflictError``) and the three hard PRB faults (``PolicyRulesBuilderError``,
# ``LLMAccessError``, ``UnparseableLLMResponseError``) all leave a provisioned-but-unusable
# service, so each rolls back what Provision created. Any other exception (e.g. an
# ``HTTPException`` from IdP focus resolution) propagates untouched — no teardown.
_ROLLBACK_ERRORS = (
    PolicyConflictError,
    PolicyRulesBuilderError,
    LLMAccessError,
    UnparseableLLMResponseError,
)


# --------------------------------------------------------------------------- #
# Seam (patched in unit tests)                                                 #
# --------------------------------------------------------------------------- #
def _config() -> Configuration:
    return Configuration.for_default_realm()


def _loggable(value: object) -> str:
    """Neutralize a value for single-line logging: coerce to ``str`` and drop CR/LF so a
    user-controlled ``service_id`` or entity name cannot forge or inject extra log lines
    (mitigates CodeQL ``py/log-injection``)."""
    return str(value).replace("\r", "").replace("\n", "")


def _rollback(config: Configuration, service_id: str, created_roles, created_scopes) -> None:
    """Compensating rollback (UC1-only): tear down exactly what Provision created on this run,
    unset the client type, then disable the client as a failed-service marker.

    ``created_roles`` / ``created_scopes`` are the **created-manifest** — only the entities this
    run added (reused-by-name entities are absent, so a role/scope another service shares is never
    removed). Each ``delete_service_*`` unmaps-then-deletes and is idempotent, so a retry that
    finds an object already gone does not crash. The disable lands **last**, after the teardown,
    so an interrupted rollback never leaves a disabled-but-still-provisioned client. Actions are
    logged at INFO."""
    service = config.get_service(service_id)
    safe_id = _loggable(service_id)
    for role in created_roles:
        config.delete_service_role(service, role)
        logger.info("UC1 rollback: deleted role %r (service %s)", _loggable(getattr(role, "name", role)), safe_id)
    for scope in created_scopes:
        config.delete_service_scope(service, scope)
        logger.info("UC1 rollback: deleted scope %r (service %s)", _loggable(getattr(scope, "name", scope)), safe_id)
    config.unset_service_type(service)
    logger.info("UC1 rollback: unset client type (service %s)", safe_id)
    config.set_service_enabled(service, False)
    logger.info("UC1 rollback: disabled client — failed-service marker (service %s)", safe_id)


def onboard_service(
    service_id: str, default_effect: RuleEffect = RuleEffect.DENY
) -> tuple[list[PolicyRule], bool, RuleEffect]:
    """Sequence Provision → Policy Builder and return ``(rules, override=False, default_effect)``.

    On any of the four typed build failures (see ``_ROLLBACK_ERRORS``) the Orchestrator runs the
    compensating :func:`_rollback` (UC1-only) and **re-raises** the original error unchanged. On
    success it re-enables the client (``set_service_enabled(service, True)`` — idempotent), which
    clears any failed-disable left by a prior attempt.

    ``default_effect`` is passed straight back to the Controller so it reaches the single
    ``compute_and_apply`` call and lands on every derived ``AgentPolicyModel``. It defaults to
    ``DENY`` (least-privilege); a caller onboarding a service that should default to ``ALLOW``
    supplies it here. This is the caller-facing surface for requesting a permissive default
    end-to-end (onboard → PCE → derived APM → OPA)."""
    provision = build_provision_graph().invoke(
        OnboardingProvisionState(trigger=Trigger(entity_id=service_id))
    )
    service_type = provision["service_type"]
    created_roles = provision["created_roles"]
    created_scopes = provision["created_scopes"]

    config = _config()
    try:
        rules = ServicePolicyBuilder.build(service_id, service_type)
    except _ROLLBACK_ERRORS:
        _rollback(config, service_id, created_roles, created_scopes)
        raise

    # Success: re-enable the client (idempotent), clearing any prior failed-disable marker.
    config.set_service_enabled(config.get_service(service_id), True)
    return rules, False, default_effect
