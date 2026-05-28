from typing import Any

from pydantic import BaseModel, ConfigDict


class User(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    username: str
    email: str | None = None
    firstName: str | None = None
    lastName: str | None = None
    enabled: bool


class RealmRole(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    description: str | None = None
    composite: bool
    clientRole: bool


class RoleMappings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    realmMappings: list[RealmRole] = []
    clientMappings: dict[str, Any] = {}


class Client(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    clientId: str
    name: str | None = None
    enabled: bool
    protocol: str | None = None
    publicClient: bool


class ClientScope(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    description: str | None = None
    protocol: str | None = None


class ClientRole(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    description: str | None = None
    composite: bool
    clientRole: bool