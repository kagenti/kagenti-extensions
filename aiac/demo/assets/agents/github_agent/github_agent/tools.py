import logging

logger = logging.getLogger(__name__)

# Module constants grouped by skill/scope per spec §5:
SOURCE_READ  = ["get_file_contents", "list_branches", "get_commit", "list_commits", "search_code"]
SOURCE_WRITE = ["create_or_update_file", "delete_file", "push_files", "create_branch"]
ISSUE_READ   = ["issue_read", "list_issues", "search_issues", "list_issue_types",
                "pull_request_read", "list_pull_requests"]
ISSUE_WRITE  = ["issue_write", "add_issue_comment", "sub_issue_write",
                "create_pull_request", "update_pull_request"]
DEFAULT_ENABLED_TOOLS = SOURCE_READ + SOURCE_WRITE + ISSUE_READ + ISSUE_WRITE

# Named-but-excluded (documented; re-enable via ENABLED_TOOLS):
EXCLUDED = ["get_me", "get_team_members", "get_teams", "run_secret_scanning",
            "create_repository", "fork_repository", "list_tags", "get_tag", "get_label"]


def enabled_tool_names(settings) -> list[str]:
    """Return the list of enabled tool names from settings or the default curated list."""
    if settings.ENABLED_TOOLS:
        return [name.strip() for name in settings.ENABLED_TOOLS.split(",")]
    return DEFAULT_ENABLED_TOOLS


def select_enabled_tools(mcp_tools, settings) -> list:
    """Filter mcp_tools keeping only items whose .name is in the enabled set.

    Logs dropped tool names at DEBUG level.
    Caller raises RuntimeError if the result is empty — not this function's job.
    """
    allowed = set(enabled_tool_names(settings))
    kept = []
    dropped = []
    for tool in mcp_tools:
        if tool.name in allowed:
            kept.append(tool)
        else:
            dropped.append(tool.name)
    if dropped:
        logger.debug("Dropping MCP tools not in enabled set: %s", dropped)
    return kept
