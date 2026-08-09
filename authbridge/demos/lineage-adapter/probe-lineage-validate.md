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

## 5. Optional visual confirmation

- DG UI per trace: `http://dg.localtest.me:8080/ui/traces/<trace_id>/flow`
  (trace ids are printed by the validators). Every trace shows a "missing
  parent" note — the dangling wire parent is by design, not a finding.
- Tape: `cd <umbrella>/glass-box/mirror && python3 tape_real.py <trace_id>` —
  the Map should show `service:httpbin.org`; the invisible legs (HTTPS, redis,
  file) do NOT appear anywhere, by definition.

## 6. Triage guide (symptom → where to look)

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

Isolated failure (one assertion, cause understood, rest of run intact): finish
the run, report it as a finding. Structural failure (pairing, orphans, guessed
links): stop and report before touching anything.

## 7. Report format

Produce exactly this:

```
LINEAGE PROBE VALIDATION — <date>, image <sidecar image tag if known>
1. probe-validate.sh:        PASS | FAIL   (paste the final capabilities block)
2. probe-cross-validate.sh:  PASS | FAIL   (paste the final cross-session block)
3. Trace ids: main <tid1>,<tid2>; cross A <tidA>, B <tidB>
4. Findings: NONE | list — each with: assertion violated, observed vs expected,
   evidence (validator lines / SQL output), isolated-or-structural, your read
   on producer (sidecar) vs consumer (derivation) vs environment.
5. Environment notes: anything retried, httpbin latency, pod restarts observed.
```

Do not summarize green as prose — paste the validators' own final blocks; they
are the evidence.
