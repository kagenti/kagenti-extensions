# session-budget demo assets

Deployable helpers for exercising the `session-budget` plugin. For plugin
configuration and mode semantics, see
[`../../docs/session-budget-plugin.md`](../../docs/session-budget-plugin.md).

> **No cluster handy?** See [`hitl-local.md`](hitl-local.md) for a
> laptop-only walkthrough of `on_exceed: pause` (Docker + Go + curl,
> no Kubernetes).

## `k8s/pause-webhook-stub.yaml`

Minimal HITL webhook that returns `{"action":"approve"}` for every POST —
enough to smoke-test `on_exceed: pause` end-to-end. Also logs each
incoming request body so you can see exactly what session-budget sent.

```bash
kubectl apply -f k8s/pause-webhook-stub.yaml
```

To exercise the deny path, edit the inline Python in the manifest to
return `{"action":"deny"}` and re-apply.

Follow the webhook stub:

```bash
kubectl logs -n "$NS" deploy/pause-webhook-stub -f
```

## Prerequisites

- **A Redis-wire-compatible store** reachable from the agent pod. Any
  Valkey/Redis deployment works; point `redis_url` at its Service.
- **`a2a-parser` on the inbound pipeline** — see the note below.

**Ambient-mesh note:** if your namespace has
`istio.io/dataplane-mode: ambient`, the datastore pod needs the
pod-level label `istio.io/dataplane-mode: none`. Ambient's ztunnel drops
non-HBONE connections with `Connection reset by peer`, and Redis RESP is
raw TCP — it can't ride HBONE. The pause webhook stub manifest already
carries the exemption.

## Configuring the plugin

Minimum pipeline for session-budget:

```yaml
pipeline:
  inbound:
    plugins:
      - name: a2a-parser         # REQUIRED — parses contextId → Session.ID
  outbound:
    plugins:
      - name: session-budget
        config:
          redis_url: "redis://valkey.team1.svc:6379"
          max_calls: 3
          max_duration_seconds: 1800
          on_exceed: pause
          pause_webhook: "http://pause-webhook-stub.team1.svc.cluster.local"
          pause_timeout: 10s
          pause_timeout_action: deny
          pause_grace_period: 5m
      - name: inference-parser   # supplies token counts to session-budget
```

**`a2a-parser` on inbound is not optional.** Without it, every request
lands in the `default` session bucket (no `Rekey` from `contextId`), so
session-budget can never distinguish sessions and cold-cache hydrate
looks for the wrong key.

## Try it end-to-end (pause mode)

Assumes an authbridge-sidecar'd agent is already running in `${NS}` with
`session-budget` (pause mode) + `a2a-parser` inbound configured per
above. Substitute your own agent, session id, and A2A payload.

```bash
NS=team1
AGENT_POD=$(kubectl -n "$NS" get pod -l app.kubernetes.io/name=<your-agent> \
  -o jsonpath='{.items[0].metadata.name}')
SESSION=demo-$RANDOM

# Substitute your Redis/Valkey pod + CLI. E.g. REDIS_POD=valkey and
# REDIS_CLI=valkey-cli, or REDIS_POD=redis-0 and REDIS_CLI=redis-cli.
REDIS_POD=valkey
REDIS_CLI=valkey-cli

# 1. Seed Redis so this session is already over budget.
kubectl -n "$NS" exec "$REDIS_POD" -- "$REDIS_CLI" HSET \
  "session-budget:$SESSION" calls 99 started_at "$(date +%s)"

# 2. Fire one A2A request with contextId = seeded session.
#    (Any callable agent works; adjust auth + payload to fit yours.)
#    $TOKEN is a Keycloak-issued bearer for the agent's inbound audience —
#    obtain via the same setup script you use for the rest of your demos
#    (see e.g. authbridge/demos/weather-agent). If your agent's inbound
#    plugin chain has no jwt-validation, omit the Authorization header.
kubectl -n "$NS" port-forward pod/"$AGENT_POD" 8000:8000 &
curl -sS -X POST http://localhost:8000/ \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":"1","method":"message/send","params":{
       "message":{"messageId":"m1","role":"user",
       "parts":[{"kind":"text","text":"hi"}],
       "contextId":"'"$SESSION"'"}}}'

# 3. Confirm the webhook was called with the right session_id.
kubectl -n "$NS" logs deploy/pause-webhook-stub | grep "$SESSION"
```

Expected: the request returns 200 (stub approves), and the webhook log
shows one POST body with `"session_id":"<SESSION>"` and `"reason":
"call limit reached: ..."`.

**Try the other modes:** swap `on_exceed: pause` for `deny` or
`observe` in the plugin config, redeploy, and repeat step 2. `deny`
returns 403 with a `budget.exceeded` body once the local cache catches
up (one request may pass first — see the cold-cache note in the
reference doc). `observe` never blocks; grep the authbridge-proxy logs
for `"budget exceeded (shadow mode)"` to see breaches.
