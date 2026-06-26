from typing import Literal
from pydantic import BaseModel, ConfigDict
from aiac.pdp.library.configuration.models import Role, Scope

class Rule(BaseModel):
    model_config  = ConfigDict(extra="ignore")
    role: Role
    scope: Scope

class PolicyObjectModel(BaseModel):
    model_config = ConfigDict(extra="ignore")
    rules: list[Rule]
    explanation: str
