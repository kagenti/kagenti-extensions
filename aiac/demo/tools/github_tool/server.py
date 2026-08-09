import os

from mcp.server.fastmcp import FastMCP

# Single editable registry — one entry per canonical UC-1 scope.
# Names match scenario.py::TOOL_SCOPES keys; descriptions are verbatim copies.
TOOLS = [
    ("source-read", "Read source repository contents: file listings and file bodies. Read-only."),
    ("source-write", "Create, modify, or delete source repository contents; commit file changes."),
    ("issues-read", "Read issues and their comment threads. Read-only."),
    ("issues-write", "Create and update issues: open, edit, comment, and close."),
]

mcp = FastMCP("github-tool", host="0.0.0.0", json_response=True, stateless_http=True)

for _tool_name, _tool_desc in TOOLS:
    def _make_stub(name: str):
        def stub() -> str:
            return f"stub: {name} not implemented in phase-1 demo"

        stub.__name__ = name.replace("-", "_")
        return stub

    mcp.add_tool(fn=_make_stub(_tool_name), name=_tool_name, description=_tool_desc)

app = mcp.streamable_http_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "9090")),
        log_level=os.getenv("LOG_LEVEL", "info").lower(),
    )
