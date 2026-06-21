from pydantic import BaseModel, ConfigDict

from aiac.pdp.library.configuration.models import Service

class Priviledge(BaseModel):
    model_config  = ConfigDict(extra="ignore")

    name: str
    services: list[Service]

class Policy(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    policy: dict[str, list[Priviledge]]
    explanation: str = ""
