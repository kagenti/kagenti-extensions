from pydantic import BaseModel


class PolicyStatement(BaseModel):
    statement_type: str
    entity_refs: list[str]


class PolicyModel(BaseModel):
    statements: list[PolicyStatement]
