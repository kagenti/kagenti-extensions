import json
import os
from pydantic_settings import BaseSettings
from pydantic import model_validator
from pydantic import Field
from typing import Literal, Optional


class Settings(BaseSettings):
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        os.getenv("LOG_LEVEL", "INFO"),
        description="Application log level",
    )
    TASK_MODEL_ID: str = Field(
        os.getenv("TASK_MODEL_ID", "ollama/ibm/granite4:latest"),
        description="The ID of the task model",
    )
    LLM_API_BASE: str = Field(
        os.getenv("LLM_API_BASE", "http://host.docker.internal:11434"),
        description="The URL for OpenAI API",
    )
    LLM_API_KEY: str = Field(os.getenv("LLM_API_KEY", "my_api_key"), description="The key for OpenAI API")
    EXTRA_HEADERS: dict = Field({}, description="Extra headers for the OpenAI API")
    MODEL_TEMPERATURE: float = Field(
        os.getenv("MODEL_TEMPERATURE", 0),
        description="The temperature for the model",
        ge=0,
    )
    MCP_URL: str = Field(
        os.getenv("MCP_URL", "http://github-tool-mcp:9090/mcp"),
        description="Endpoint for the MCP server",
    )
    MCP_TIMEOUT: int = Field(os.getenv("MCP_TIMEOUT", 600), description="Timeout in seconds for MCP server connection")
    ENABLED_TOOLS: Optional[str] = Field(
        os.getenv("ENABLED_TOOLS", None),
        description="Comma-separated list of tool names to enable (overrides the default curated allow-list)",
    )

    # auth variables for token validation
    ISSUER: Optional[str] = Field(os.getenv("ISSUER", None), description="The issuer for incoming JWT tokens")
    AGENT_ENDPOINT: Optional[str] = Field(
        os.getenv("AGENT_ENDPOINT", None),
        description="Override the URL advertised in the agent card",
    )
    SERVICE_PORT: int = Field(os.getenv("PORT", 8000), description="Port on which the service will run.")
    GITHUB_TOKEN: Optional[str] = Field(
        os.getenv("GITHUB_TOKEN", None),
        description="If not using agent with authorization, the default Github token to use",
    )

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

    @model_validator(mode="after")
    def validate_extra_headers(self) -> "Settings":
        if os.getenv("EXTRA_HEADERS"):
            try:
                self.EXTRA_HEADERS = json.loads(os.getenv("EXTRA_HEADERS"))
            except json.JSONDecodeError:
                raise ValueError("EXTRA_HEADERS must be a valid JSON string")

        return self


settings = Settings()  # type: ignore[call-arg]
