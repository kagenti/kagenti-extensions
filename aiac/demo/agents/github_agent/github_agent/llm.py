from crewai import LLM
from github_agent.config import Settings


class CrewLLM:
    def __init__(self, config: Settings):
        kwargs = {}
        if config.EXTRA_HEADERS is not None and None not in config.EXTRA_HEADERS:
            kwargs["extra_headers"] = config.EXTRA_HEADERS

        # For Ollama models, pass num_ctx to set the context window size.
        # Ollama defaults to 2048 tokens which is too small for agent workflows.
        if config.TASK_MODEL_ID.startswith(("ollama/", "ollama_chat/")):
            kwargs["num_ctx"] = 8192

        self.llm = LLM(
            model=config.TASK_MODEL_ID,
            base_url=config.LLM_API_BASE,
            api_key=config.LLM_API_KEY,
            temperature=config.MODEL_TEMPERATURE,
            **kwargs,
        )
