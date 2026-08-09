"""lineage-probe: ONE app that exercises every lineage capability at once.

One image, three roles selected by the ROLE env var:

* ROLE=front  ENTRY AGENT. On POST /probe/{tag}: one real LLM call, then
              LEGS concurrent A2A requests to BACK_URL/{tag}-leg{i} — each
              submitted to a ThreadPoolExecutor with a *sync* HTTP client,
              so context must survive a real thread hand-off (the shim's
              `threading` instrumentor is load-bearing here, not decorative).

* ROLE=back   FAN-IN WORKER. On POST /probe/{tag}: hold the request open
              for HOLD_MS — guaranteeing all concurrent same-trace inbounds
              overlap in flight, the shape where trace membership cannot
              pair an outbound to its inbound — then make one MCP tools/call
              to TOOL_URL and one real LLM call, both tag-stamped, both
              through the ThreadPoolExecutor (a second thread hand-off).

* ROLE=tool   MCP LEAF. On POST /mcp/{tag}: answer the JSON-RPC tools/call
              with a result echoing the term. Bare JSON-RPC over HTTP — the
              sidecar's mcp-parser classifies by body, not by path.

One driver run (N concurrent turns, distinct caller-minted traceparents)
therefore validates, in a single topology:
  (1) concurrent traces      — N distinct single-rooted forests, no cross-talk;
  (2) thread propagation     — pairing survives executor hand-offs at BOTH pods;
  (3) inbound→outbound links — back receives LEGS same-trace inbounds held
      open together, and each of its 2×LEGS outbounds (tool + LLM) must
      parent on exactly the inbound whose tag it carries.

The tag rides in url.path on every hop (recorded by the sidecar with no
parser dependency — the per-span ground truth) and in every body (payload
capture). The app is deliberately un-instrumented: stock Starlette + httpx;
trace context flows only via the deploy-time propagate-only OTel shim.
"""

import os
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

ROLE = os.environ.get("ROLE", "front")
BACK_URL = os.environ.get("BACK_URL", "").rstrip("/")
TOOL_URL = os.environ.get("TOOL_URL", "").rstrip("/")
LLM_API_BASE = os.environ.get(
    "LLM_API_BASE", "http://host.containers.internal:11434/v1"
).rstrip("/")
LLM_MODEL = os.environ.get("LLM_MODEL", "qwen2.5:7b")
HOLD_MS = int(os.environ.get("HOLD_MS", "2000"))
LEGS = int(os.environ.get("LEGS", "3"))
TIMEOUT = httpx.Timeout(120.0)


def a2a_body(tag: str, text: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": tag,
        "method": "message/send",
        "params": {
            "message": {
                "role": "user",
                "messageId": f"m-{tag}",
                "parts": [{"kind": "text", "text": text}],
            }
        },
    }


def call_llm(tag: str) -> str:
    """One real chat completion; the tag is the payload ground truth."""
    body = {
        "model": LLM_MODEL,
        "messages": [
            {
                "role": "user",
                "content": f"Reply with exactly this token and nothing else: {tag}",
            }
        ],
        "temperature": 0,
        "max_tokens": 20,
    }
    with httpx.Client(timeout=TIMEOUT) as client:
        resp = client.post(f"{LLM_API_BASE}/chat/completions", json=body)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def call_tool(tag: str) -> str:
    body = {
        "jsonrpc": "2.0",
        "id": tag,
        "method": "tools/call",
        "params": {"name": "fact", "arguments": {"term": tag}},
    }
    with httpx.Client(timeout=TIMEOUT) as client:
        resp = client.post(f"{TOOL_URL}/mcp/{tag}", json=body)
    resp.raise_for_status()
    return resp.json()["result"]["content"][0]["text"]


def call_back(tag: str, i: int) -> dict:
    leg = f"{tag}-leg{i}"
    with httpx.Client(timeout=TIMEOUT) as client:
        resp = client.post(
            f"{BACK_URL}/{leg}", json=a2a_body(leg, f"probe leg {leg}")
        )
    resp.raise_for_status()
    return resp.json()


def probe(request: Request) -> JSONResponse:
    """Sync handler on purpose: Starlette runs it in a worker thread, and the
    executor below adds a second hand-off — both must carry the context."""
    tag = request.path_params["tag"]

    if ROLE == "front":
        llm_text = call_llm(tag)
        with ThreadPoolExecutor(max_workers=LEGS) as pool:
            futures = [pool.submit(call_back, tag, i) for i in range(LEGS)]
            legs = [f.result() for f in futures]
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": tag,
                "result": {"role": "front", "tag": tag, "llm": llm_text, "legs": legs},
            }
        )

    if ROLE == "back":
        # Hold ALL concurrent same-trace inbounds open before any outbound
        # fires — the fan-in shape the tracestate stamp exists to survive.
        time.sleep(HOLD_MS / 1000)
        with ThreadPoolExecutor(max_workers=2) as pool:
            tool_f = pool.submit(call_tool, tag)
            llm_f = pool.submit(call_llm, tag)
            tool_text, llm_text = tool_f.result(), llm_f.result()
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": tag,
                "result": {"role": "back", "tag": tag, "tool": tool_text, "llm": llm_text},
            }
        )

    return JSONResponse({"error": f"role {ROLE} does not serve /probe"}, status_code=404)


async def mcp(request: Request) -> JSONResponse:
    """The tool leaf makes no outbound calls, so async needs no thread story."""
    tag = request.path_params["tag"]
    try:
        doc = await request.json()
    except Exception:
        doc = {}
    term = doc.get("params", {}).get("arguments", {}).get("term", tag)
    return JSONResponse(
        {
            "jsonrpc": "2.0",
            "id": doc.get("id"),
            "result": {"content": [{"type": "text", "text": f"fact({term})=FIXED"}]},
        }
    )


async def health(_: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "role": ROLE})


app = Starlette(
    routes=[
        Route("/probe/{tag}", probe, methods=["POST"]),
        Route("/mcp/{tag}", mcp, methods=["POST"]),
        Route("/health", health, methods=["GET"]),
    ]
)


def run() -> None:
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))


if __name__ == "__main__":
    run()
