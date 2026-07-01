from aiac.idp.configuration.models import Role, Scope
from pydantic import BaseModel, ConfigDict


class Rule(BaseModel):
    model_config  = ConfigDict(extra="ignore")
    role: Role
    scope: Scope

class PolicyObjectModel(BaseModel):
    model_config = ConfigDict(extra="ignore")
    rules: list[Rule]
    explanation: str
