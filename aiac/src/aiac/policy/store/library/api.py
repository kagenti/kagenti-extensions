import os

import requests
from dotenv import load_dotenv

from aiac.policy.model.models import AgentPolicyModel, PolicyModel

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

_BASE_URL = os.getenv("AIAC_POLICY_STORE_URL", "http://127.0.0.1:7074")


def _base_url() -> str:
    return os.getenv("AIAC_POLICY_STORE_URL", "http://127.0.0.1:7074")


def _check(response: requests.Response) -> None:
    if not response.ok:
        raise RuntimeError(f"Policy Store error {response.status_code}")


def get_policy() -> PolicyModel:
    resp = requests.get(f"{_base_url()}/policy")
    _check(resp)
    return PolicyModel.model_validate(resp.json())


def get_agent_policy(agent_id: str) -> AgentPolicyModel:
    resp = requests.get(f"{_base_url()}/policy/agents/{agent_id}")
    _check(resp)
    return AgentPolicyModel.model_validate(resp.json())


def apply_policy(model: PolicyModel) -> None:
    resp = requests.post(f"{_base_url()}/policy", json=model.model_dump())
    _check(resp)


def apply_agent_policy(agent_id: str, model: AgentPolicyModel) -> None:
    resp = requests.post(f"{_base_url()}/policy/agents/{agent_id}", json=model.model_dump())
    _check(resp)


def delete_agent_policy(agent_id: str) -> None:
    resp = requests.delete(f"{_base_url()}/policy/agents/{agent_id}")
    _check(resp)


def delete_policy() -> None:
    resp = requests.delete(f"{_base_url()}/policy")
    _check(resp)
