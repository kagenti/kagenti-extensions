import os

import requests
from dotenv import load_dotenv

# Scope / Role / ServiceType are re-exported by aiac.policy.model.models (it imports them
# from aiac.idp.configuration.models), so the whole model surface comes from one module.
from aiac.policy.model.models import Role, Scope, ServicePolicyModel, ServiceType
from aiac.policy.model_store.keying import encode_service_id

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

# Per-request timeout (seconds) for every Policy Store HTTP call. Without it a hung store
# would block the caller indefinitely. Tunable via ``AIAC_HTTP_TIMEOUT``.
_HTTP_TIMEOUT = float(os.getenv("AIAC_HTTP_TIMEOUT", "10"))


def _base_url() -> str:
    return os.getenv("AIAC_POLICY_MODEL_STORE_URL", "http://127.0.0.1:7074")


def _check(response: requests.Response) -> None:
    if not response.ok:
        raise RuntimeError(f"Policy Store error {response.status_code}")


def _fresh_empty(service_id: str) -> ServicePolicyModel:
    # The "engine creates a fresh model on 404" convention: the first time a service is
    # seen the store has no row, so callers get an empty SPM to append to. ``service_type``
    # is required by the model but genuinely unknown here; the PCE re-seeds it (along with
    # ``owned_roles`` / ``owned_scopes``) from the IdP catalog before it is ever consulted,
    # so the placeholder below never reaches a policy decision.
    return ServicePolicyModel(
        service_id=service_id,
        service_type=ServiceType.AGENT,
        owned_roles=[],
        owned_scopes=[],
        inbound_allow_rules=[],
        inbound_deny_rules=[],
    )


def get_service_policy(service_id: str) -> ServicePolicyModel:
    resp = requests.get(
        f"{_base_url()}/policy/services/{encode_service_id(service_id)}", timeout=_HTTP_TIMEOUT
    )
    if resp.status_code == 404:
        return _fresh_empty(service_id)
    _check(resp)
    return ServicePolicyModel.model_validate(resp.json())


def get_service_policy_by_scope(scope: Scope) -> ServicePolicyModel | None:
    # Singular: a scope has exactly one owning service (Assumption 2). Pure sugar over the
    # by-id read — no dedicated HTTP route. A scope with no resolved owner has no SPM.
    if not scope.serviceId:
        return None
    return get_service_policy(scope.serviceId)


def get_service_policies_by_role(role: Role) -> list[ServicePolicyModel]:
    # The one genuinely new route. Returns every SPM whose inbound_rules reference role.id —
    # including stale role->service mappings the live IdP no longer reflects (which
    # override-purge needs). [] when none match.
    resp = requests.get(
        f"{_base_url()}/policy/services", params={"role": role.id}, timeout=_HTTP_TIMEOUT
    )
    _check(resp)
    return [ServicePolicyModel.model_validate(item) for item in resp.json()]


def apply_service_policy(service_id: str, spm: ServicePolicyModel) -> None:
    resp = requests.post(
        f"{_base_url()}/policy/services/{encode_service_id(service_id)}",
        json=spm.model_dump(),
        timeout=_HTTP_TIMEOUT,
    )
    _check(resp)


def delete_service_policy(service_id: str) -> None:
    resp = requests.delete(
        f"{_base_url()}/policy/services/{encode_service_id(service_id)}", timeout=_HTTP_TIMEOUT
    )
    _check(resp)


def clear_service_policies() -> None:
    # Drop every SPM in the store. The collection-root DELETE (no service_id segment) —
    # distinct from delete_service_policy's by-id path. Intended for test harnesses that
    # need a clean slate before a run; clearing an already-empty store is a no-op.
    resp = requests.delete(f"{_base_url()}/policy/services", timeout=_HTTP_TIMEOUT)
    _check(resp)
