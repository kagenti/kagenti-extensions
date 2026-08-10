import json
import logging
import re
import sys

from crewai_tools.adapters.tool_collection import ToolCollection

from github_agent.agents import GithubAgents
from github_agent.config import Settings, settings
from github_agent.data_types import GithubQueryInfo
from github_agent.event import Event

logger = logging.getLogger(__name__)
logging.basicConfig(level=settings.LOG_LEVEL, stream=sys.stdout, format="%(levelname)s: %(message)s")


def _parse_prereq_from_raw(raw: str) -> GithubQueryInfo:
    """Parse GithubQueryInfo from raw LLM text when instructor/pydantic parsing fails.

    Some Ollama models don't produce structured tool calls that crewai's instructor
    integration expects. This fallback extracts JSON from the raw text output.
    """
    # Only matches flat JSON (no nested braces). Sufficient for the current
    # GithubQueryInfo schema; revisit if the model gains nested fields.
    json_match = re.search(r"\{[^{}]*\}", raw)
    if json_match:
        try:
            data = json.loads(json_match.group())
            return GithubQueryInfo(**data)
        except (json.JSONDecodeError, ValueError):
            pass

    logger.warning("Could not parse prereq JSON from raw output: %s", raw)
    return GithubQueryInfo()


class GithubAgent:
    def __init__(
        self,
        config: Settings,
        eventer: Event = None,
        mcp_toolkit: ToolCollection = None,
        logger=None,
    ):
        self.agents = GithubAgents(config, mcp_toolkit)
        self.eventer = eventer
        self.logger = logger or logging.getLogger(__name__)

    async def _send_event(self, message: str, final: bool = False):
        logger.info(message)
        if self.eventer:
            await self.eventer.emit_event(message, final)
        else:
            logger.warning("No event handler registered")

    def extract_user_input(self, body):
        content = body[-1]["content"]
        latest_content = ""

        if isinstance(content, str):
            latest_content = content
        else:
            for item in content:
                if item["type"] == "text":
                    latest_content += item["text"]
                else:
                    self.logger.warning(f"Ignoring content with type {item['type']}")

        return latest_content

    async def _get_prereq_output(self, query: str) -> GithubQueryInfo:
        """Run the prereq crew and extract GithubQueryInfo from raw text output.

        We avoid using crewai's output_pydantic because it relies on instructor's
        tool-call-based parsing, which fails with Ollama models that don't produce
        structured tool calls. Instead, we ask the LLM for JSON and parse it ourselves.
        """
        try:
            await self.agents.prereq_id_crew.kickoff_async(
                inputs={"request": query, "repo": "", "owner": "", "ref": "", "path": "", "numbers": []}
            )
            raw = self.agents.prereq_identifier_task.output.raw
            self.logger.info(f"Prereq raw output: {raw}")
            return _parse_prereq_from_raw(raw)
        except Exception as e:
            self.logger.warning(f"Prereq crew failed: {e}")
            return GithubQueryInfo()

    async def execute(self, user_input):
        query = self.extract_user_input(user_input)
        await self._send_event("Evaluating requirements...")
        prereq = await self._get_prereq_output(query)

        # Validation gates — return helpful message without tool call when unmet
        if prereq.numbers:
            if not prereq.owner or not prereq.repo:
                return "When supplying issue or PR numbers, you must provide both a repository name and owner."
        if prereq.repo:
            if not prereq.owner:
                return "When supplying a repository name, you must also provide an owner of the repo."

        await self._send_event("Searching GitHub...")
        await self.agents.crew.kickoff_async(
            inputs={
                "request": query,
                "owner": prereq.owner or "",
                "repo": prereq.repo or "",
                "ref": prereq.ref or "",
                "path": prereq.path or "",
                "numbers": prereq.numbers or [],
            }
        )
        output = self.agents.github_query_task.output
        if output is None or output.raw is None:
            return "The agent produced no output for this request."
        return output.raw
