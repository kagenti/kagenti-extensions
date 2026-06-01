from typing import Any

from pydantic import BaseModel, ConfigDict


class Subject(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    username: str
    email: str | None = None
    firstName: str | None = None
    lastName: str | None = None
    enabled: bool


class Role(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    description: str | None = None
    composite: bool
    clientRole: bool


class Assignments(BaseModel):
    model_config = ConfigDict(extra="ignore")

    realmMappings: list[Role] = []
    serviceMappings: dict[str, Any] = {}


class Service(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    clientId: str
    name: str | None = None
    description: str | None = None
    enabled: bool
    protocol: str | None = None
    publicClient: bool


class Scope(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    description: str | None = None
    protocol: str | None = None


class Permission(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    description: str | None = None
    composite: bool
    clientRole: bool
