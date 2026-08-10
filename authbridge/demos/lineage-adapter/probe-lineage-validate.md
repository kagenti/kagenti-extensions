# probe-lineage-validate.md — run the lineage probe and report results

You are validating the two-span lineage pipeline end to end using the probe
topology. This file is self-contained: follow it top to bottom, then produce
the report at the end. **Ground rules:**

- The probe ASSERTS exact shapes, including expected ABSENCES. Do not "fix"
  numbers to make a run pass — a mismatch is a finding, not an inconvenience.
- Do NOT modify code to get to green. If something fails, capture the evidence
  (full validator output + the queries below), classify it, and report it.
- Drive traffic IN-cluster only (the scripts do this). Never port-forward to
  the probe services — that bypasses the sidecar and invalidates the run.

## 1. What you are validating (one paragraph)

Every HTTP exchange through an AuthBridge sidecar emits two facts-only spans
(request + response, paired by `lineage.exchange.id`); the DG processor
re-derives each trace into an interaction forest. The probe is ONE app in three
roles — `probe-front` (real LLM call + external HTTP/HTTPS legs + 3 concurrent
A2A legs via a ThreadPoolExecutor), `probe-back` (holds all same-trace inbounds
open, then per inbound one MCP `tools/call` + one LLM call through a second
executor), `probe-tool` (bare JSON-RPC MCP leaf) — plus `probe-redis` (stock
redis, deliberately OUTSIDE lineage) and a shared PVC. It walks the boundary of
sidecar visibility and asserts both sides of it.

## 2. Prerequisites (check, don't assume)

Run these; all must hold before you continue:

```bash
kubectl get pods -n team1 | grep -E 'probe-(front|back|tool|redis)'   # 4 pods; probe-redis 1/1, others 2/2
kubectl get pods -n data-governance                                    # postgres, receiver(s), interactions, ui — all Running
curl -s -o /dev/null -w '%{http_code}\n' http://dg.localtest.me:8080/ui/traces   # 200
kubectl exec -n team1 lineage-driver2 -- curl -s -o /dev/null -w '%{http_code}\n' --max-time 20 http://httpbin.org/anything/egress-check   # 200 (external egress)
curl -s http://localhost:11434/v1/models 2>/dev/null | head -c 80      # host Ollama up (qwen2.5:7b)
```

If the probe pods are missing or stale, deploy from the kit directory:

```bash
cd authbridge/demos/lineage-adapter
./deploy-fleet.sh probe-redis probe-tool probe-back probe-front
```

(Builds the probe image + shim, pulls redis:7, kind-loads under
`docker.io/library/*`, deploys raw → tool → agents. Podman host: images load
via archive automatically; do not use `kind load docker-image`.)

Notes:
- The driver pod `lineage-driver2` is created by the validators if absent.
- External egress uses live `httpbin.org`. If it is down, the run will fail on
  capability (4) — that is an environment finding, not a lineage bug. Override
  with `EXT_HTTP_URL`/`EXT_HTTPS_URL`/`EXT_HOST` only if ys approved a substitute.

## 3. Main validation — `./probe-validate.sh`

One command, ~2 minutes. It fires N=2 concurrent turns with caller-minted
traceparents and asserts, per trace, from the DG store:

| # | Capability | Exact expectation |
|---|---|---|
| 1 | Concurrent traces | 2 distinct trace ids, each a single-rooted forest: `ix=12 roots=1 entry-roots=1 orphans=0 anchors=12 dup=0 callee=agent:probe-front` |
| 2 | Thread propagation | implied by pairing: context survives executors at BOTH pods |
| 3 | Exact inbound→outbound pairing | pairing table `11/11 OK`, zero `MISATTRIBUTED`; rule: out-tag == in-tag or in-tag+"-legN" |
| 4 | External egress, presence AND absence | `app http=200 https=200` and `derived 1 x service:httpbin.org (want 1)` — 0 means the visible leg broke; 2 means HTTPS leaked in (TLS-passthrough gap closed silently: STOP and report, do not adjust) |
| 5 | Tool identity echo | `tool callee 3 x tool:probe-tool` (never a host-key fallback) |
| — | Derived kinds | `agent_request=4 llm_chat_prompt=4 tool_call_arguments=3` per trace; NO `mcp_lifecycle_*`/`tool_discovery_*` (the probe mints single bare `tools/call`s — MCP handshake noise appearing here is a finding) |

Success line: `ALL CAPABILITIES VALIDATED ✔`, exit 0.

Expected outbound composition per trace (the 11): front = 1 LLM + 1 ext-HTTP +
3 A2A legs; back = 3 MCP + 3 LLM. The HTTPS leg adds 0 — invisible BY DESIGN.

## 4. Cross-session validation — `./probe-cross-validate.sh`

One command. Trace A (`/stash`) sends exact bytes front→back `/echo`; back
persists them to the shared-PVC file AND redis. A later trace B (`/replay`)
reads BOTH stores from the other pod and re-sends the bytes over the visible
hop. Asserts:

| Check | Exact expectation |
|---|---|
| (a) app round-trips | one sha256 equal across all four readings (stash = echo = file = redis), `stores_match=true` |
| (b) tree shape | each trace `ix=2 roots=1 orphans=0`; the two traces DISCONNECTED (today's honest derivation — a link here would mean something is guessing) |
| (c) invisible hops | `redis-derived rows across both traces: 0`; file I/O has no wire at all |
| (d) the hook | exactly ≥1 shared `interaction_legs.payload_hash` between the two traces, whose payload content contains `cross-session stash <tag>` — the content-addressed join that seeds the data-at-rest lineage workstream |

Success line: `CROSS-SESSION VALIDATED ✔`, exit 0.

## 5. Issue-155 read surface — destination / http / identity / feed

The interactions API enriches every interaction at read time from the span
pair's stored facts (wire contract v1.5.1 + lab-dg commits `c653360`/`ff7ccd3`).
Validate it against ONE of the main-validation traces (`$TID` = a trace id
printed by `probe-validate.sh`). Requires the probe sidecars to be on a
v1.5.1+ image (they emit `url.scheme`) and the DG pod on `ff7ccd3`+.

```bash
curl -s "http://dg.localtest.me:8080/api/traces/$TID/interactions" | python3 -c '
import json,sys
d=json.load(sys.stdin)["interactions"]
for ix in d:
    dest=ix["destination"] or {}; http=ix["http"] or {}
    print((ix["summary"] or "")[:44], "|", dest.get("url"), "| int:", dest.get("internal"),
          "|", http.get("method"), http.get("status_code"), http.get("outcome"),
          "| sub:", ix["principal_sub"], "| sess:", ix["session_id"])'
```

| Field | Exact expectation on a main-validation trace |
|---|---|
| `destination.url` | composed `http://…` on ALL 12 interactions — the 11 outbound AND the inbound root (`http://probe-front.team1.svc.cluster.local:8080/probe/<tag>`, `internal: true`; the listener records the inbound authority since sidecar v1.5.2). Spans stored by a pre-v1.5.2 sidecar render the inbound row path-only with `url=null` — correct for those spans, not a finding |
| `destination.internal` | `false` for `httpbin.org` and for NOTHING else; `true` for `probe-back`/`probe-tool` svc authorities and `host.containers.internal:11434` |
| `http` | `POST/200/ok` everywhere except the httpbin leg's `GET/200/ok`; any other `outcome` is a finding |
| `principal_sub` | `null` on EVERY row (no JWT on probe paths) — asserted absence; a value appearing means something fabricates identity |
| `session_id` | `null` on EVERY row (the probe mints bare `message/send`, no `contextId`) — asserted absence |
| span names | `/api/traces/$TID/interactions/<iid>/spans` rows carry `name` (e.g. `probe-front a2a message/send`); entity-evidence rows have NO `name` key |
| feed | `GET /api/interactions?since_seq=0&limit=2` → 2 rows + integer `next_seq`; passing `next_seq` back returns strictly later rows; cursoring reaches all 12 of the trace's interactions; `limit` > 1000 or a non-integer cursor → 400 |

## 6. Optional visual confirmation

- DG UI per trace: `http://dg.localtest.me:8080/ui/traces/<trace_id>/flow`
  (trace ids are printed by the validators). Every trace shows a "missing
  parent" note — the dangling wire parent is by design, not a finding.
- Tape: `cd <umbrella>/glass-box/mirror && python3 tape_real.py <trace_id>` —
  the Map should show `service:httpbin.org`; the invisible legs (HTTPS, redis,
  file) do NOT appear anywhere, by definition.

## 7. Triage guide (symptom → where to look)

| Symptom | Likely cause |
|---|---|
| curl to /probe times out / non-200 | app or LLM down: `kubectl logs -n team1 deploy/probe-front -c agent`; check Ollama at host.containers.internal:11434 |
| `ext_http`/`ext_https` empty or ≠200 | httpbin.org unreachable — environment, retry/report as env finding |
| `derived 0 x service:httpbin.org` | outbound sidecar not seeing plaintext egress — real lineage regression |
| `derived 2 x service:httpbin.org` | HTTPS became visible — the TLS-passthrough assumption changed; STOP, report |
| pairing `MISATTRIBUTED` rows | parent-stamping regression (tracestate `dg-parent` channel) — the core contract; STOP, report with the pairing table |
| `ix` ≠ 12 / orphans ≠ 0 | derivation or span-loss issue; dump `SELECT count(*) FROM spans WHERE trace_id='…'` (expect 36) to split producer vs consumer |
| kinds include `mcp_lifecycle_*` | something added MCP handshake traffic (client library crept in) — report |
| redis rows > 0 | port-exclude bypass broke (`OUTBOUND_PORTS_EXCLUDE=6379` missing from proxy-init) — check `kubectl get pod -n team1 <probe-back-pod> -o yaml \| grep -A2 OUTBOUND_PORTS` |
| (d) no shared hash | payload capture or content-addressing changed — compare `interaction_legs.payload_hash` per trace manually |
| stale pods after deploy | probe images are local `IfNotPresent`; re-run deploy-fleet (it restarts + waits). NEVER roll non-probe `:latest` agents casually — they pull upstream on restart |
| `destination.url` null | sidecar image predates the fact it needs — v1.5.1 added `url.scheme` (outbound URLs), v1.5.2 added the inbound authority (inbound URLs); check the request span's attributes to see which is missing. Host+path with null url is the correct rendering for such spans, not a guess |
| `internal` flag wrong | consumer-side heuristic drifted (`_host_is_internal` in `retrieval/interactions.py`) — vocabulary bug, not a producer issue |
| `http.status_code`/`outcome` null with a response leg present | response-span attribute read broke (they live on the RESPONSE span, not the anchor) — consumer regression |
| feed rows missing / cursor stuck | `interaction_legs.seq` stream issue — compare `GET /api/interactions` against a direct `SELECT max(seq) FROM interaction_legs` |

Isolated failure (one assertion, cause understood, rest of run intact): finish
the run, report it as a finding. Structural failure (pairing, orphans, guessed
links): stop and report before touching anything.

## 8. Report format

Produce exactly this:

```
LINEAGE PROBE VALIDATION — <date>, image <sidecar image tag if known>
1. probe-validate.sh:        PASS | FAIL   (paste the final capabilities block)
2. probe-cross-validate.sh:  PASS | FAIL   (paste the final cross-session block)
3. issue-155 read surface:   PASS | FAIL   (destination/internal/http/nulls/names/feed per §5)
4. Trace ids: main <tid1>,<tid2>; cross A <tidA>, B <tidB>
5. Findings: NONE | list — each with: assertion violated, observed vs expected,
   evidence (validator lines / SQL output), isolated-or-structural, your read
   on producer (sidecar) vs consumer (derivation) vs environment.
6. Environment notes: anything retried, httpbin latency, pod restarts observed.
```

Do not summarize green as prose — paste the validators' own final blocks; they
are the evidence.
