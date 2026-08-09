"""
Module for A2A Agent.
"""

import asyncio
import concurrent.futures
import logging
import os
import sys
import traceback

import uvicorn
from crewai_tools import MCPServerAdapter
from crewai_tools.adapters.tool_collection import ToolCollection

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events.event_queue import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.helpers import new_task_from_user_message, new_text_part
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    TaskState,
    SecurityScheme,
    HTTPAuthSecurityScheme,
)
from starlette.applications import Starlette

from github_agent.config import settings, Settings
from github_agent.event import Event
from github_agent.tools import select_enabled_tools

logger = logging.getLogger(__name__)
logging.basicConfig(level=settings.LOG_LEVEL, stream=sys.stdout, format="%(levelname)s: %(message)s")


def get_agent_card(host: str, port: int):
    """Returns the Agent Card for the Github Agent."""
    capabilities = AgentCapabilities(streaming=True)

    skill_source = AgentSkill(
        id="source_operations",
        name="Source repository operations",
        description="Browse and search code; read, create, and modify repository file contents, branches, and commits.",
        tags=["git", "github", "source", "repositories", "files", "branches", "commits"],
        examples=[
            "Show the README of kagenti/kagenti",
            "List the branches of owner/repo",
            "Create a branch and commit a fix to owner/repo",
        ],
    )

    skill_issue = AgentSkill(
        id="issue_operations",
        name="Issue & PR tracker operations",
        description="Read, search, create, and update issues, comments, sub-issues, and pull requests.",
        tags=["git", "github", "issues", "pull-requests"],
        examples=[
            "List open issues in kubernetes/kubernetes",
            "Open an issue in owner/repo titled ...",
            "Summarise PR #42 in owner/repo",
        ],
    )

    if settings.AGENT_ENDPOINT:
        url = settings.AGENT_ENDPOINT.rstrip("/") + "/"
    else:
        url = f"http://{host}:{port}/"

    return AgentCard(
        name="Github agent",
        description=(
            "Autonomous Agent acting on a user's behalf against source repositories and an issue tracker. "
            "It inspects and changes repository source contents and reads, creates, and updates issues and their threads."
        ),
        supported_interfaces=[AgentInterface(url=url, protocol_binding="JSONRPC")],
        version="1.0.0",
        default_input_modes=["text"],
        default_output_modes=["text"],
        capabilities=capabilities,
        skills=[skill_source, skill_issue],
        security_schemes={
            "Bearer": SecurityScheme(
                http_auth_security_scheme=HTTPAuthSecurityScheme(
                    scheme="bearer", bearer_format="JWT", description="OAuth 2.0 JWT token"
                )
            )
        },
    )


class A2AEvent(Event):
    """
    A class to handle events for A2A Agent.

    Attributes:
        task_updater (TaskUpdater): The task updater instance.
    """

    def __init__(self, task_updater: TaskUpdater):
        """
        Initializes the A2AEvent instance.

        Args:
            task_updater (TaskUpdater): The task updater instance.
        """
        self.task_updater = task_updater

    async def emit_event(self, message: str, final: bool = False) -> None:
        """
        Emits an event with the given message.

        Args:
            message (str): The event message.
            final (bool): Whether the event is final. Defaults to False.
        """
        logger.info("Emitting event %s", message)

        if final:
            parts = [new_text_part(message)]
            await self.task_updater.add_artifact(parts)
            await self.task_updater.complete()
        else:
            await self.task_updater.update_status(
                TaskState.TASK_STATE_WORKING,
                self.task_updater.new_agent_message([new_text_part(message)]),
            )


class GithubExecutor(AgentExecutor):
    """
    A class to handle execution for A2A Agent.
    """

    async def _run_agent(self, messages: dict, settings: Settings, event_emitter: Event, toolkit: ToolCollection):
        from github_agent.main import GithubAgent

        github_agent = GithubAgent(
            config=settings,
            eventer=event_emitter,
            mcp_toolkit=toolkit,
        )
        result = await github_agent.execute(messages)
        await event_emitter.emit_event(result, True)

    async def execute(self, context: RequestContext, event_queue: EventQueue):
        """
        Executes the task.

        Args:
            context (RequestContext): The request context.
            event_queue (EventQueue): The event queue instance.

        Returns:
            None
        """
        # If GITHUB_TOKEN is set, pass it as Bearer header to MCP.
        # If not set, assume AuthBridge handles auth transparently (envoy injects tokens).
        headers = {}
        if settings.GITHUB_TOKEN:
            headers["Authorization"] = f"Bearer {settings.GITHUB_TOKEN}"
        elif context.call_context and (context.call_context.state or {}).get("headers", {}).get("authorization"):
            headers["Authorization"] = context.call_context.state["headers"]["authorization"]
        else:
            logging.warning(
                "No GITHUB_TOKEN or inbound Authorization header; outbound requests will be unauthenticated"
            )

        user_input = [context.get_user_input()]
        task = context.current_task
        if not task:
            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)
        task_updater = TaskUpdater(event_queue, task.id, task.context_id)
        event_emitter = A2AEvent(task_updater)
        messages = []
        for message in user_input:
            messages.append(
                {
                    "role": "User",
                    "content": message,
                }
            )

        # Hook up MCP tools
        try:
            if settings.MCP_URL:
                logging.info("Connecting to MCP server at %s", settings.MCP_URL)

                server_params = {
                    "url": settings.MCP_URL,
                    "transport": "streamable-http",
                    "headers": headers,
                }
                adapter = MCPServerAdapter(server_params, connect_timeout=settings.MCP_TIMEOUT)
                # MCPServerAdapter.__enter__/__exit__ perform blocking MCP I/O; run them off the
                # event loop so we don't stall other async tasks. They MUST run on the SAME thread
                # (the adapter binds MCP session state to the entering thread), so use a dedicated
                # single-worker executor rather than asyncio.to_thread's shared pool, which could
                # otherwise dispatch __enter__ and __exit__ to different workers.
                loop = asyncio.get_running_loop()
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as mcp_exec:
                    mcp_tools = await loop.run_in_executor(mcp_exec, adapter.__enter__)
                    try:
                        curated_tools = select_enabled_tools(mcp_tools, settings)
                        if not curated_tools:
                            raise RuntimeError(
                                "No enabled tools found from the GitHub MCP server. "
                                "Check the ENABLED_TOOLS setting and ensure the server is reachable."
                            )
                        await self._run_agent(messages, settings, event_emitter, curated_tools)
                    finally:
                        await loop.run_in_executor(mcp_exec, adapter.__exit__, None, None, None)
            else:
                await self._run_agent(messages, settings, event_emitter, None)

        except Exception as e:
            traceback.print_exc()
            await event_emitter.emit_event(
                f"I'm sorry I was unable to fulfill your request. I encountered the following exception: {str(e)}", True
            )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """
        Not implemented
        """
        raise Exception("cancel not supported")


def run():
    """
    Runs the A2A Agent application.
    """
    agent_card = get_agent_card(host="0.0.0.0", port=settings.SERVICE_PORT)
    request_handler = DefaultRequestHandler(
        agent_executor=GithubExecutor(),
        task_store=InMemoryTaskStore(),
        agent_card=agent_card,
    )
    routes = create_jsonrpc_routes(request_handler, rpc_url="/", enable_v0_3_compat=True)
    routes += create_agent_card_routes(agent_card)
    routes += create_agent_card_routes(agent_card, card_url="/.well-known/agent.json")
    app = Starlette(routes=routes)
    uvicorn.run(app, host="0.0.0.0", port=settings.SERVICE_PORT)
