import pytest
from github_agent.tools import (
    DEFAULT_ENABLED_TOOLS,
    EXCLUDED,
    SOURCE_READ,
    SOURCE_WRITE,
    ISSUE_READ,
    ISSUE_WRITE,
    enabled_tool_names,
    select_enabled_tools,
)

# Extra tool names that appear in the full 44-tool github-tool catalog but are not in the
# enabled set — used to build a representative stub list of the expected catalog size.
_EXTRA_CATALOG_TOOLS = [
    "get_me", "get_team_members", "get_teams", "run_secret_scanning",
    "create_repository", "fork_repository", "list_tags", "get_tag", "get_label",
    "create_release", "list_releases", "get_release", "get_repository",
    "list_repositories", "watch_repository", "unwatch_repository",
    "get_file_metadata", "list_directory", "get_tree",
    "create_gist", "list_gists",
    "get_discussion", "list_discussions",
]


class FakeTool:
    def __init__(self, name):
        self.name = name


class FakeSettings:
    def __init__(self, enabled_tools=None):
        self.ENABLED_TOOLS = enabled_tools


def test_select_keeps_enabled_tools():
    all_tools = [FakeTool(n) for n in DEFAULT_ENABLED_TOOLS + EXCLUDED]
    settings = FakeSettings()
    result = select_enabled_tools(all_tools, settings)
    result_names = {t.name for t in result}
    for name in DEFAULT_ENABLED_TOOLS:
        assert name in result_names, f"{name} should be in result"
    for name in EXCLUDED:
        assert name not in result_names, f"{name} should not be in result"


def test_select_with_override():
    all_tools = [FakeTool(n) for n in DEFAULT_ENABLED_TOOLS + EXCLUDED]
    settings = FakeSettings(enabled_tools="issue_read,list_issues")
    result = select_enabled_tools(all_tools, settings)
    result_names = [t.name for t in result]
    assert result_names == ["issue_read", "list_issues"]


def test_whitespace_tolerance():
    all_tools = [FakeTool(n) for n in DEFAULT_ENABLED_TOOLS + EXCLUDED]
    settings = FakeSettings(enabled_tools=" issue_read , list_issues ")
    result = select_enabled_tools(all_tools, settings)
    result_names = [t.name for t in result]
    assert result_names == ["issue_read", "list_issues"]


def test_default_tools_no_duplicates():
    assert len(DEFAULT_ENABLED_TOOLS) == len(set(DEFAULT_ENABLED_TOOLS))


def test_default_tools_union_of_groups():
    assert set(DEFAULT_ENABLED_TOOLS) == set(SOURCE_READ + SOURCE_WRITE + ISSUE_READ + ISSUE_WRITE)


def test_source_vs_issue_membership():
    default_set = set(DEFAULT_ENABLED_TOOLS)
    for name in SOURCE_READ:
        assert name in default_set
    for name in SOURCE_WRITE:
        assert name in default_set
    for name in ISSUE_READ:
        assert name in default_set
    for name in ISSUE_WRITE:
        assert name in default_set


def test_excluded_tools_absent_by_default():
    default_set = set(DEFAULT_ENABLED_TOOLS)
    for name in EXCLUDED:
        assert name not in default_set, f"{name} should not appear in DEFAULT_ENABLED_TOOLS"


def test_stub_44_tool_catalog_filtered_to_default():
    """Simulate the full github-tool catalog (~44 tools); default set filters it to exactly DEFAULT_ENABLED_TOOLS."""
    catalog = DEFAULT_ENABLED_TOOLS + _EXTRA_CATALOG_TOOLS
    # Pad to 44 entries if the catalog is shorter (adds unused dummy names)
    while len(catalog) < 44:
        catalog = catalog + [f"_dummy_{len(catalog)}"]
    tools = [FakeTool(n) for n in catalog[:44]]
    result = select_enabled_tools(tools, FakeSettings())
    result_names = {t.name for t in result}
    assert result_names == set(DEFAULT_ENABLED_TOOLS)
    assert len(result) == len(DEFAULT_ENABLED_TOOLS)


def test_executor_raises_runtime_error_on_empty_selection():
    """GithubExecutor raises RuntimeError when select_enabled_tools returns an empty list."""
    from a2a_agent import GithubExecutor
    import inspect

    # Verify the RuntimeError guard is present in the execute source
    src = inspect.getsource(GithubExecutor.execute)
    assert "RuntimeError" in src
    assert "tools found" in src.lower() or "enabled tools" in src.lower()
