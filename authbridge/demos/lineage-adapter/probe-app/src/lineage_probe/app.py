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

EXTENSIONS (asserted by probe-validate.sh / probe-cross-validate.sh):

* External legs (front, in /probe): one plaintext HTTP GET and one HTTPS GET
  to the SAME out-of-cluster endpoint. Plaintext derives an interaction
  (callee = peer host); HTTPS derives NOTHING (TLS passthrough — asserted
  as an absence, not left unmentioned).
* Cross-session flow: POST /stash/{tag} (front, trace A) sends exact bytes
  to back's /echo, which persists them to a shared file AND redis (both
  invisible to lineage — redis port-excluded RESP, file I/O has no wire).
  POST /replay/{tag} (front, later trace B) reads both stores and re-sends
  the bytes over the visible hop. The two traces derive disconnected trees
  linked ONLY by the content-addressed payload hash.
"""

import hashlib
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

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

# External legs (front): the SAME endpoint over plaintext and TLS. Plaintext
# MUST derive an interaction (callee = peer host); TLS MUST derive nothing —
# the sidecar's documented passthrough gap, asserted as an absence. Empty
# value disables the leg.
EXT_HTTP_URL = os.environ.get("EXT_HTTP_URL", "").rstrip("/")
EXT_HTTPS_URL = os.environ.get("EXT_HTTPS_URL", "").rstrip("/")

# Cross-session flow: back WRITES the raw exchange body to a shared file and
# to redis (RESP — excluded from the sidecar redirect, invisible to lineage);
# front READS both in a LATER trace. Writer and reader are different pods on
# purpose: two distinct redis clients, one shared PVC.
REDIS_URL = os.environ.get("REDIS_URL", "")
SHARE_DIR = os.environ.get("SHARE_DIR", "")
BACK_ECHO_URL = os.environ.get("BACK_ECHO_URL", "").rstrip("/")


def redis_client():
    import redis  # imported lazily: the tool role never touches redis

    return redis.Redis.from_url(REDIS_URL)


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


def call_external(base: str, tag: str) -> int:
    """One GET to a real out-of-cluster endpoint, tag in the path."""
    with httpx.Client(timeout=TIMEOUT) as client:
        resp = client.get(f"{base}/{tag}")
    return resp.status_code


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
        ext_http = call_external(EXT_HTTP_URL, tag) if EXT_HTTP_URL else None
        ext_https = call_external(EXT_HTTPS_URL, tag) if EXT_HTTPS_URL else None
        with ThreadPoolExecutor(max_workers=LEGS) as pool:
            futures = [pool.submit(call_back, tag, i) for i in range(LEGS)]
            legs = [f.result() for f in futures]
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": tag,
                "result": {
                    "role": "front",
                    "tag": tag,
                    "llm": llm_text,
                    "ext_http": ext_http,
                    "ext_https": ext_https,
                    "legs": legs,
                },
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


async def echo(request: Request) -> JSONResponse:
    """back: the cross-session WRITER. Persist the raw inbound body — the very
    bytes the sidecar captured as this exchange's request payload — to the
    shared file and to redis. Both stores are invisible to lineage (file I/O
    has no wire; redis is port-excluded); the visible A2A hop is the capture."""
    tag = request.path_params["tag"]
    raw = await request.body()
    if SHARE_DIR:
        Path(SHARE_DIR, f"{tag}.json").write_bytes(raw)
    if REDIS_URL:
        redis_client().set(f"probe:{tag}", raw)
    return JSONResponse(
        {
            "jsonrpc": "2.0",
            "id": tag,
            "result": {
                "role": "back",
                "tag": tag,
                "wrote": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            },
        }
    )


def stash(request: Request) -> JSONResponse:
    """front, trace A: mint a deterministic A2A body and send its EXACT bytes
    to back's /echo — the sidecar captures them as a payload, back persists
    them at rest. Returns the bytes' sha256 as app-level ground truth."""
    tag = request.path_params["tag"]
    body = json.dumps(a2a_body(tag, f"cross-session stash {tag}"), sort_keys=True).encode()
    with httpx.Client(timeout=TIMEOUT) as client:
        resp = client.post(
            f"{BACK_ECHO_URL}/{tag}", content=body,
            headers={"content-type": "application/json"},
        )
    resp.raise_for_status()
    return JSONResponse(
        {
            "jsonrpc": "2.0",
            "id": tag,
            "result": {
                "role": "front",
                "tag": tag,
                "stashed_sha256": hashlib.sha256(body).hexdigest(),
                "echo": resp.json()["result"],
            },
        }
    )


def replay(request: Request) -> JSONResponse:
    """front, trace B (a LATER trace): the cross-session READER. Read the
    stashed bytes back from BOTH stores mid-flow — file and redis, two hops
    lineage cannot see — and USE them: re-send the exact bytes to back over
    the visible A2A hop, so trace B captures a payload byte-identical to
    trace A's. Content-addressed storage then links the two traces by hash."""
    tag = request.path_params["tag"]
    file_bytes = Path(SHARE_DIR, f"{tag}.json").read_bytes() if SHARE_DIR else b""
    redis_bytes = redis_client().get(f"probe:{tag}") or b"" if REDIS_URL else b""
    with httpx.Client(timeout=TIMEOUT) as client:
        resp = client.post(
            f"{BACK_ECHO_URL}/{tag}", content=file_bytes,
            headers={"content-type": "application/json"},
        )
    resp.raise_for_status()
    return JSONResponse(
        {
            "jsonrpc": "2.0",
            "id": tag,
            "result": {
                "role": "front",
                "tag": tag,
                "file_sha256": hashlib.sha256(file_bytes).hexdigest(),
                "redis_sha256": hashlib.sha256(redis_bytes).hexdigest(),
                "stores_match": file_bytes == redis_bytes and len(file_bytes) > 0,
                "echo": resp.json()["result"],
            },
        }
    )


async def health(_: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "role": ROLE})


app = Starlette(
    routes=[
        Route("/probe/{tag}", probe, methods=["POST"]),
        Route("/mcp/{tag}", mcp, methods=["POST"]),
        Route("/echo/{tag}", echo, methods=["POST"]),
        Route("/stash/{tag}", stash, methods=["POST"]),
        Route("/replay/{tag}", replay, methods=["POST"]),
        Route("/health", health, methods=["GET"]),
    ]
)


def run() -> None:
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))


if __name__ == "__main__":
    run()
