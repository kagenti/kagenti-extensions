import json
from pydantic import BaseModel, Field, field_validator


class GithubQueryInfo(BaseModel):
    owner: str | None = Field(None, description="The repository owner or organization.")
    repo: str | None = Field(None, description="The repository name.")
    ref: str | None = Field(None, description="Branch, tag, or sha if named.")
    path: str | None = Field(None, description="File path if named.")
    numbers: list[int] | None = Field(
        None, description="Issue or PR numbers mentioned by the user."
    )

    @field_validator("numbers", mode="before")
    @classmethod
    def coerce_string_to_list(cls, v):
        """Small LLMs often serialize arrays as strings in tool call args."""
        if isinstance(v, str):
            try:
                parsed = json.loads(v)
                if isinstance(parsed, list):
                    return parsed
            except (json.JSONDecodeError, ValueError):
                pass
        return v
