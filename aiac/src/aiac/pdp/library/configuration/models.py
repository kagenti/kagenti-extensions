from typing import Literal

from pydantic import BaseModel, ConfigDict


class Subject(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    username: str
    email: str | None = None
    firstName: str | None = None
    lastName: str | None = None
    enabled: bool
    roles: list["Role"] = []


class Role(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    description: str | None = None
    composite: bool
    childRoles: list["Role"] = []
    mappedScopes: list["Scope"] = []


class Service(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str | None = None
    description: str | None = None
    enabled: bool
    type: Literal["Agent", "Tool"] | None = None
    roles: list["Role"] = []
    scopes: list["Scope"] = []


class Scope(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    description: str | None = None


Subject.model_rebuild()
Role.model_rebuild()
Service.model_rebuild()
