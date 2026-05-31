"""Tests for aiac_agent.config.llm_config.load_llm_config (env-only, no network)."""

from pathlib import Path

import pytest

from config.llm_config import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_MAX_TOKENS,
    DEFAULT_TEMPERATURE,
    DEFAULT_TIMEOUT,
    load_llm_config_from_env as load_llm_config,
)


_LLM_VARS = (
    "LLM_MODEL",
    "LLM_ENDPOINT",
    "LLM_API_KEY",
    "LLM_TEMPERATURE",
    "LLM_MAX_TOKENS",
    "LLM_TIMEOUT",
    "LLM_MAX_RETRIES",
)


@pytest.fixture
def clean_llm_env(monkeypatch):
    """Strip every LLM_* var so each test starts from a clean slate."""
    for key in _LLM_VARS:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


MISSING = Path("/nonexistent/llm.env")  # Skip the dotenv branch.


def test_loads_required_and_optional_vars(clean_llm_env):
    clean_llm_env.setenv("LLM_MODEL", "openai/gpt-4o")
    clean_llm_env.setenv("LLM_ENDPOINT", "https://example/v1")
    clean_llm_env.setenv("LLM_API_KEY", "sk-test")

    cfg = load_llm_config(MISSING)
    assert cfg.model == "openai/gpt-4o"
    assert cfg.endpoint == "https://example/v1"
    assert cfg.api_key == "sk-test"


def test_endpoint_and_key_are_optional(clean_llm_env):
    clean_llm_env.setenv("LLM_MODEL", "openai/gpt-4o")

    cfg = load_llm_config(MISSING)
    assert cfg.endpoint is None
    assert cfg.api_key is None


def test_empty_endpoint_and_key_treated_as_unset(clean_llm_env):
    clean_llm_env.setenv("LLM_MODEL", "openai/gpt-4o")
    clean_llm_env.setenv("LLM_ENDPOINT", "")
    clean_llm_env.setenv("LLM_API_KEY", "")

    cfg = load_llm_config(MISSING)
    assert cfg.endpoint is None
    assert cfg.api_key is None


def test_model_is_stripped(clean_llm_env):
    clean_llm_env.setenv("LLM_MODEL", "  anthropic/claude-sonnet-4-6  ")
    clean_llm_env.setenv("LLM_ENDPOINT", "https://example.com")
    clean_llm_env.setenv("LLM_API_KEY", "test-key")

    cfg = load_llm_config(MISSING)
    assert cfg.model == "anthropic/claude-sonnet-4-6"


def test_rejects_missing_model(clean_llm_env):
    with pytest.raises(ValueError, match="LLM_MODEL"):
        load_llm_config(MISSING)


def test_rejects_blank_model(clean_llm_env):
    clean_llm_env.setenv("LLM_MODEL", "   ")
    with pytest.raises(ValueError, match="LLM_MODEL"):
        load_llm_config(MISSING)


def test_default_generation_params(clean_llm_env):
    clean_llm_env.setenv("LLM_MODEL", "openai/gpt-4o")

    cfg = load_llm_config(MISSING)
    assert cfg.temperature == DEFAULT_TEMPERATURE
    assert cfg.max_tokens == DEFAULT_MAX_TOKENS
    assert cfg.timeout == DEFAULT_TIMEOUT
    assert cfg.retries == DEFAULT_MAX_RETRIES


def test_overrides_generation_params_from_env(clean_llm_env):
    clean_llm_env.setenv("LLM_MODEL", "openai/gpt-4o")
    clean_llm_env.setenv("LLM_TEMPERATURE", "0.7")
    clean_llm_env.setenv("LLM_MAX_TOKENS", "1024")
    clean_llm_env.setenv("LLM_TIMEOUT", "60")
    clean_llm_env.setenv("LLM_MAX_RETRIES", "5")

    cfg = load_llm_config(MISSING)
    assert cfg.temperature == 0.7
    assert cfg.max_tokens == 1024
    assert cfg.timeout == 60
    assert cfg.retries == 5


def test_blank_generation_params_fall_back_to_defaults(clean_llm_env):
    clean_llm_env.setenv("LLM_MODEL", "openai/gpt-4o")
    clean_llm_env.setenv("LLM_TEMPERATURE", "")
    clean_llm_env.setenv("LLM_MAX_TOKENS", "")
    clean_llm_env.setenv("LLM_TIMEOUT", "")
    clean_llm_env.setenv("LLM_MAX_RETRIES", "")

    cfg = load_llm_config(MISSING)
    assert cfg.temperature == DEFAULT_TEMPERATURE
    assert cfg.max_tokens == DEFAULT_MAX_TOKENS
    assert cfg.timeout == DEFAULT_TIMEOUT
    assert cfg.retries == DEFAULT_MAX_RETRIES


def test_loads_from_dotenv_file(clean_llm_env, tmp_path):
    env_file = tmp_path / "llm.env"
    env_file.write_text(
        "LLM_MODEL=ollama/llama3\n"
        "LLM_ENDPOINT=http://localhost:11434\n"
        "LLM_TEMPERATURE=0.3\n"
    )

    cfg = load_llm_config(env_file)
    assert cfg.model == "ollama/llama3"
    assert cfg.endpoint == "http://localhost:11434"
    assert cfg.temperature == 0.3
