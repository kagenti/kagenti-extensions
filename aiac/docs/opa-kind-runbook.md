# OPA Kind Cluster Runbook — AIAC github-agent (inbound + outbound)

> **Pre-release:** This document shows how OPA can be experimented with prior
> to its release as part of the Rossoctl system. On release, this document
> should be updated accordingly.

This is the AIAC-scoped companion to
[`authbridge/docs/opa-kind-runbook.md`](../../authbridge/docs/opa-kind-runbook.md).
The underlying mechanism — OPA as an AuthBridge pipeline plugin, policy
distributed via `bundle-service`, enforcement via the `AuthorizationPolicy`
CRD — is identical. This document gives the **exact, copy-paste** steps to run
the AIAC scenario end-to-end on a local Kind cluster, using the two helper
scripts that wire OPA in and out:

- [`scripts/opa-kind-enable.sh`](../../scripts/opa-kind-enable.sh) — rebuilds
  the `authbridge-proxy` image from the current tree, loads it into Kind, and
  wires the `opa` plugin (plus the parser set) into **both** the inbound and
  outbound pipeline of every `team1` agent.
- [`scripts/opa-kind-restore.sh`](../../scripts/opa-kind-restore.sh) — reverts
  the pipeline to its shipped state (no OPA overlay) and restarts the agents.

The scenario itself uses one agent (`github-agent` in namespace `team1`) and
its downstream tool (`github-tool`):

- **`dev-user` is the allowed user, `alice` is the blocked user.** `dev-user`
  is the canonical scenario username from
  [`docs/specs/integration-test/policy-pipeline.md`](specs/integration-test/policy-pipeline.md).
- **Inbound** authorization is enforced by a **client-scoped**
  `AuthorizationPolicy` targeting `github-agent` alone.
- **Outbound** shows the token-exchange → OPA leg: the agent's call to
  `github-tool` is exchanged for a `github-tool`-audience token, and OPA sees a
  delegation chain plus a synthesized `input.identity`.

## Architecture

```
Inbound:   caller ─► jwt-validation ─► OPA ─► github-agent app
Outbound:  github-agent app ─► token-exchange ─► OPA ─► github-tool
```

Policies are distributed via the bundle service used by every AuthBridge
workload: `http://bundle-service.rossoctl-system.svc.cluster.local:8080`.
On the outbound leg OPA is placed **after** `token-exchange` so policies can
read the delegation chain (see [Part B](#part-b--outbound-token-exchange--opa)).

---

## Prerequisites

- A Kind cluster named `rossoctl` with the `rossoctl` platform installed and
  `github-agent` + `github-tool` deployed in namespace `team1`.
- The two sibling repo clones the enable/restore scripts need:
  - `OPERATOR_DIR` → `rossoctl/operator` clone (default: `../operator`)
  - `ROSSOCTL_DIR` → `rossoctl/rossoctl` clone, i.e. the Helm chart
    (default: `../rossoctl`)
- `kubectl`, `helm`, `kind`, and `docker` (or `podman`) on `PATH`.
- The `rossoctl` Keycloak realm has `dev-user` and `alice` users with
  **password == username**, and the `rossoctl` client has Direct Access Grants
  enabled plus a `username → sub` protocol mapper. In this cluster this is
  already done cluster-wide — it is a one-time Keycloak change, not per-agent.

All commands below are run from the repo root (`cortex/`).

---

## Step 1 — Enable OPA in both legs

```bash
OPERATOR_DIR=../operator ROSSOCTL_DIR=../rossoctl ./scripts/opa-kind-enable.sh
```

This rebuilds `localhost/authbridge:local` from the current tree, loads it into
the `rossoctl` Kind cluster, and `helm upgrade`s the chart with a temporary
overlay that inserts `opa` (after `token-exchange` on the outbound leg) and the
parser set into every `team1` agent's pipeline. It does **not** modify
`charts/rossoctl/values.yaml` on disk.

Confirm OPA is wired into **both** legs (expect **2**):

```bash
kubectl get configmap authbridge-runtime-config -n team1 \
  -o jsonpath='{.data.config\.yaml}' | grep -c 'name: opa'
# 2
```

---

## Step 2 — Verify the starting point

```bash
# github-agent is 2/2 (app + authbridge-proxy sidecar)
kubectl get pods -n team1 -l app.kubernetes.io/name=github-agent

# bundle-service is up and serving the shipped global policy
kubectl get pods -n rossoctl-system -l app=bundle-service   # 1/1 Running
kubectl get authorizationpolicy -n rossoctl-system          # 'default', scope global

# github-agent's SPIFFE ID — this is what the client-scoped policy targets
kubectl exec -n team1 deploy/github-agent -c authbridge-proxy -- cat /shared/client-id.txt
# spiffe://localtest.me/ns/team1/sa/github-agent
```

---

# Part A — Inbound authorization

Proves inbound OPA authorization for `github-agent` using a **client-scoped**
policy (`spec.scope: client`) so the rule affects only this one agent.

> **How client-scope targeting works.** `bundle-service` looks up a
> client-scope CR by **`metadata.name` + `metadata.namespace`**, matched
> against the ServiceAccount segment of the caller's SPIFFE ID
> (`spiffe://<trust-domain>/ns/<namespace>/sa/<name>`). `spec.clientID` is
> **not** consulted by that lookup — it's a print-column convenience field. So
> the example CR is named `github-agent` (matching `sa/github-agent`), and
> `clientID` is the short name `"github-agent"` (the CRD validates it against a
> DNS-label regex that rejects `spiffe://` and `/`).

## A.1 — Verify the dev-user token carries the right `sub`

```bash
curl -s -X POST "http://keycloak.localtest.me:8080/realms/rossoctl/protocol/openid-connect/token" \
     -d client_id=rossoctl -d username=dev-user -d password=dev-user -d grant_type=password -d scope=openid \
  | python3 -c 'import sys,json,base64;t=json.load(sys.stdin)["access_token"].split(".")[1];t+="="*(-len(t)%4);print("sub =",json.loads(base64.urlsafe_b64decode(t)).get("sub"))'
# sub = dev-user
```

## A.2 — Probe helper

`github-agent` is only reachable in-cluster, so probe from a throwaway pod. The
helper mints a user token and posts a JSON-RPC method the agent doesn't
implement — enough to reach the app and get a fast response without triggering
the CrewAI/tool flow:

```bash
probe_as() {   # usage: probe_as dev-user | probe_as alice
  local user="$1"
  local KC=http://keycloak.localtest.me:8080
  local TOK
  TOK=$(curl -s -X POST "$KC/realms/rossoctl/protocol/openid-connect/token" \
         -d client_id=rossoctl -d "username=$user" -d "password=$user" \
         -d grant_type=password -d scope=openid \
       | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
  kubectl run "probe-$user-$RANDOM" --rm -i --restart=Never --image=curlimages/curl:8.10.1 \
    -n team1 --env="TOK=$TOK" -- sh -c \
    'curl -s -m 15 -w "\nHTTP_CODE:%{http_code}\n" \
       -X POST http://github-agent.team1.svc.cluster.local:8080/ \
       -H "Content-Type: application/json" -H "Authorization: Bearer $TOK" \
       -d "{\"jsonrpc\":\"2.0\",\"id\":\"1\",\"method\":\"ping/nonexistent\",\"params\":{}}"'
}
```

Baseline — before any client policy, both users reach the app:

```bash
probe_as dev-user
# {"error":{"code":-32601,"message":"Method not found"},"id":"1","jsonrpc":"2.0"}
# HTTP_CODE:200

probe_as alice
# {"error":{"code":-32601,"message":"Method not found"},"id":"1","jsonrpc":"2.0"}
# HTTP_CODE:200
```

`HTTP_CODE:200` with a JSON-RPC `-32601` body means the request passed
`jwt-validation` and OPA and reached the app — the app rejected the unknown
method, which is expected and irrelevant to authorization.

> **Don't test with `/.well-known/agent-card.json`** — it matches
> `jwt-validation`'s bypass list (`/.well-known/*`, `/healthz`, `/readyz`,
> `/livez`, `/metrics`) and returns `200` with **no token**, never reaching
> OPA. **Don't test with a real `message/send` task** either — it drives the
> CrewAI flow and can hang for minutes if `github-tool` is unhealthy. The
> `ping/nonexistent` probe above reaches OPA and returns instantly.

## A.3 — Apply the client-scoped policy

```bash
kubectl apply -f aiac/docs/examples/opa-team1-policy.yaml
```

`bundle-service` rebuilds the `team1` bundle on the CR change; `github-agent`'s
OPA polls the bundle on its own interval, so allow **~20–30 s** before testing.

## A.4 — Test: dev-user allowed, alice blocked

```bash
probe_as dev-user
# {"error":{"code":-32601,"message":"Method not found"},"id":"1","jsonrpc":"2.0"}
# HTTP_CODE:200            — reaches the app: allowed

probe_as alice
# {"error":"policy.forbidden","message":"policy denied","plugin":"opa"}
# HTTP_CODE:403            — blocked by OPA, never reaches the app
```

> If `alice` still returns `200` right after applying, OPA hasn't polled the
> new bundle yet — wait a few seconds and retry.

## A.5 — The inbound OPA input, exactly

With `decision_logs.console: true` (set by the enable overlay), every decision
is logged by the `authbridge-proxy` sidecar. Capture the inbound input:

```bash
POD=$(kubectl get pod -n team1 -l app.kubernetes.io/name=github-agent -o jsonpath='{.items[0].metadata.name}')
kubectl logs -n team1 "$POD" -c authbridge-proxy --tail=500 \
  | grep 'path=authbridge/inbound/request' | tail -1
```

For the `dev-user` probe the plugin builds this `input` document (rendered as
JSON; the log prints it in Go `map[...]` form):

```json
{
  "direction": "inbound",
  "method": "POST",
  "path": "/",
  "host": "github-agent.team1.svc.cluster.local:8080",
  "headers": {
    "accept": "*/*",
    "content-length": "66",
    "content-type": "application/json",
    "user-agent": "curl/8.10.1"
  },
  "identity": {
    "subject": "dev-user",
    "client_id": "rossoctl",
    "scopes": [
      "agent-team1-weather-service-advanced-aud",
      "agent-team1-github-tool-aud",
      "openid",
      "agent-team1-github-agent-aud",
      "agent-team1-weather-tool-advanced-aud",
      "profile",
      "email"
    ]
  }
}
```

- `identity` comes from the **validated inbound JWT** (`jwt-validation` runs
  before OPA). `subject` is the JWT `sub` claim — here `dev-user`, via the
  realm's `username → sub` mapper. `client_id` is the token's client (`rossoctl`
  in this probe). `scopes` are the token's granted scopes.
- Credential headers (`authorization`, `cookie`, …) are **redacted** from
  `headers` — use `identity` for auth decisions.

The policy ([`opa-team1-policy.yaml`](examples/opa-team1-policy.yaml)) keys on
`input.identity.subject`: `dev-user` maps to a role whose scopes are allowed →
`allow: true`; `alice` has no role → `allow: false`. The decision appears in
the same log line as `result`:

```
result="map[allow:true client_ok:true ns_ok:true]"     # dev-user
result="map[allow:false ns_ok:true]"                    # alice (client_ok never set → denied)
```

---

# Part B — Outbound token-exchange + OPA

The agent's outbound call to `github-tool` is intercepted by the forward proxy.
`token-exchange` matches the route, mints a `github-tool`-audience token, and
records a **delegation hop**; OPA (placed after it) then sees both
`input.delegation` and a synthesized `input.identity`.

## B.1 — Add the github-tool outbound route

Add a route for `github-tool` to the `authproxy-routes` ConfigMap (this keeps
the existing weather route):

```bash
kubectl patch configmap authproxy-routes -n team1 --type merge -p "$(python3 -c '
import json
print(json.dumps({"data":{"routes.yaml":
"""- host: \"weather-tool-advanced-mcp\"
  target_audience: \"spiffe://localtest.me/ns/team1/sa/weather-tool-advanced\"
  token_scopes: \"openid weather-tool-exchange-aud\"
- host: \"github-tool\"
  target_audience: \"spiffe://localtest.me/ns/team1/sa/github-tool\"
  token_scopes: \"openid agent-team1-github-tool-aud\"
"""}}))')"
```

- `target_audience` is the RFC 8693 `audience` — the `github-tool` SPIFFE ID.
- `token_scopes` is the requested `scope`; `agent-team1-github-tool-aud` is the
  realm client-scope whose audience mapper stamps the `github-tool` audience.

## B.2 — Grant github-agent the exchange scope

For the `client_credentials` exchange to succeed, the github-agent Keycloak
client must have `agent-team1-github-tool-aud` as an **optional** client scope:

```bash
KC=http://keycloak.localtest.me:8080
ADMIN=$(curl -s -X POST "$KC/realms/master/protocol/openid-connect/token" \
  -d client_id=admin-cli -d username=admin -d password=admin -d grant_type=password \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')

# github-agent's registered client UUID (its clientId is its SPIFFE ID)
CID=$(curl -s -H "Authorization: Bearer $ADMIN" "$KC/admin/realms/rossoctl/clients" \
  | python3 -c 'import sys,json;print(next(c["id"] for c in json.load(sys.stdin) if c["clientId"].endswith("/sa/github-agent")))')

# the client-scope that stamps the github-tool audience
SID=$(curl -s -H "Authorization: Bearer $ADMIN" "$KC/admin/realms/rossoctl/client-scopes" \
  | python3 -c 'import sys,json;print(next(s["id"] for s in json.load(sys.stdin) if s["name"]=="agent-team1-github-tool-aud"))')

curl -s -o /dev/null -w "assign scope HTTP %{http_code}\n" -X PUT -H "Authorization: Bearer $ADMIN" \
  "$KC/admin/realms/rossoctl/clients/$CID/optional-client-scopes/$SID"
# assign scope HTTP 204
```

## B.3 — Restart github-agent to load the route

Routes are read once at startup, so restart the pod:

```bash
kubectl delete pod -n team1 -l app.kubernetes.io/name=github-agent
kubectl wait --for=condition=ready pod -n team1 -l app.kubernetes.io/name=github-agent --timeout=120s
```

## B.4 — Probe the outbound leg as dev-user

The github-agent app container (`agent`) is configured with
`HTTP_PROXY=127.0.0.1:8081` (the AuthBridge forward proxy) and has `python3`.
Drive an outbound MCP call through it, carrying a `dev-user` bearer — the token
`token-exchange` uses as the RFC 8693 `subject_token`:

```bash
POD=$(kubectl get pod -n team1 -l app.kubernetes.io/name=github-agent -o jsonpath='{.items[0].metadata.name}')
TOK=$(curl -s -X POST "http://keycloak.localtest.me:8080/realms/rossoctl/protocol/openid-connect/token" \
  -d client_id=rossoctl -d username=dev-user -d password=dev-user -d grant_type=password -d scope=openid \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')

cat > /tmp/probe.py <<PY
import urllib.request, urllib.error, json
tok = """$TOK"""
op = urllib.request.build_opener(urllib.request.ProxyHandler({"http": "http://127.0.0.1:8081"}))
body = json.dumps({"jsonrpc":"2.0","id":"1","method":"tools/list","params":{}}).encode()
req = urllib.request.Request("http://github-tool:9090/", data=body,
    headers={"Content-Type":"application/json","Authorization":"Bearer "+tok})
try:
    r = op.open(req, timeout=15); print("HTTP", r.status); print(r.read().decode())
except urllib.error.HTTPError as e: print("HTTPError", e.code); print(e.read().decode())
PY
kubectl exec -i -n team1 "$POD" -c agent -- python3 - < /tmp/probe.py
# HTTP 200
# {"error":{"code":-32000,"data":{"error":"policy.forbidden","plugin":"opa"},"message":"policy denied"},"id":"1","jsonrpc":"2.0"}
```

> **The example CR's `outbound/request.rego` denies this `tools/list` probe** —
> and because the outbound pipeline includes `mcp-parser`, that denial is
> surfaced the MCP-correct way: a **JSON-RPC 2.0 error frame at HTTP 200**
> (`error.code: -32000`, `error.data.plugin: "opa"`), not an HTTP error status.
> The forward proxy renders a `Reject` for an MCP JSON-RPC request (one with a
> `method` and an `id`) as an application-layer error frame so the caller's MCP
> client sees a single failed tool call rather than a transport break — see
> `writeMCPRejection` in
> `authbridge/authlib/listener/httpx/render.go`. The request is **denied and
> never reaches `github-tool`**; the `HTTP 200` is only the JSON-RPC transport
> envelope. Classify the outcome by the response **body** (an `error` frame =
> denied, a `result` frame = allowed), not the HTTP status.
>
> The rule admits a call only when the delegated user's role and the target
> service both list the request's `input.mcp.params.name` (the invoked tool).
> A `tools/list` call carries no `params.name`, so neither gate matches and
> `allow` is `false`. A non-MCP-shaped rejection (no parser, or a JSON-RPC
> *notification* with no `id`) instead falls through to a plain HTTP `403`; a
> `token-exchange` failure surfaces as `503` before OPA is even consulted. To
> see the full allow path (a `result` frame at HTTP 200), apply only the inbound
> tier of the CR, or drive a real tool invocation whose tool name is present in
> `subject_role_scopes` and `target_scopes` in the outbound rego.

## B.5 — The outbound OPA input, exactly

```bash
kubectl logs -n team1 "$POD" -c authbridge-proxy --tail=200 \
  | grep 'path=authbridge/outbound/request' | tail -1
```

The plugin builds this `input` document:

```json
{
  "direction": "outbound",
  "method": "POST",
  "path": "/",
  "host": "github-tool:9090",
  "headers": {
    "accept-encoding": "identity",
    "connection": "close",
    "content-length": "67",
    "content-type": "application/json",
    "user-agent": "Python-urllib/3.12"
  },
  "identity": {
    "subject": "dev-user",
    "client_id": "spiffe://localtest.me/ns/team1/sa/github-agent",
    "scopes": ["openid", "agent-team1-github-tool-aud"],
    "service_id": "spiffe://localtest.me/ns/team1/sa/github-tool"
  },
  "delegation": {
    "origin": "dev-user",
    "actor": "dev-user",
    "depth": 1,
    "chain": [
      {
        "subject_id": "dev-user",
        "audience": "spiffe://localtest.me/ns/team1/sa/github-tool",
        "scopes": ["openid", "agent-team1-github-tool-aud"],
        "strategy": "token-exchange",
        "from_cache": false,
        "timestamp": "2026-08-04T07:56:56Z"
      }
    ]
  },
  "mcp": {
    "method": "tools/list"
  }
}
```

Key differences from the inbound input, and how the outbound `identity` is
built:

- There is **no validated JWT** on the outbound leg. Instead, when
  `token-exchange` mints the downstream token it records a delegation hop, and
  OPA synthesizes `input.identity` **in the same shape as inbound** so policies
  can branch on `input.identity` uniformly on both legs:
  - `subject` = the delegated caller (`delegation.origin`), decoded
    best-effort from the incoming bearer's `sub` — here `dev-user`.
  - `client_id` = the **agent's own client** (`/shared/client-id.txt`), i.e.
    the party performing the exchange — **not** the target audience.
  - `scopes` = the scopes the downstream token was minted with (the last hop).
  - `service_id` = the **downstream service** the token was minted for (the last
    hop's target `audience` — here the `github-tool` SPIFFE ID). This mirrors the
    inbound identity, where `jwt-validation` surfaces the validated JWT's
    audience; on the outbound leg the equivalent "who is this token for" signal
    is the exchange target, exposed as `service_id`. A policy keys on it via
    `target_scopes[input.identity.service_id]`. Omitted when the last hop is a
    non-exchange hop that recorded no audience.
- `input.delegation` carries the full RFC 8693 chain for policies that need
  per-hop detail (`audience`, `strategy`, `from_cache`, `depth`).
- `input.mcp` is present because the probe sent a real MCP body (`tools/list`).
  Parser sections (`mcp` / `a2a` / `inference`) appear **only** when the body
  matches that parser's protocol — a non-MCP body carries no `input.mcp`.

The example CR ([`opa-team1-policy.yaml`](examples/opa-team1-policy.yaml))
carries an `outbound/request.rego` that keys entirely on fields the live plugin
emits on this leg: the synthesized `input.identity.subject`,
`input.identity.service_id` (the exchange target, added to the outbound identity
as shown above), and `input.mcp.params.name` (the specific tool being invoked).
It gates **per tool**: allowing only when the delegated user's role **and** the
target service both admit the invoked tool — an AND across the user→tool and
service→tool gates. The `subject_role_scopes` / `target_scopes` maps in the
example are keyed by the actual MCP tool names exposed by the deployed
github-tool (`aiac/demo/assets/tools/github_tool`): `source-read`,
`source-write`, `issues-read`, `issues-write`. (Because the gate is on
`params.name`, MCP methods that don't invoke a specific tool — like the
`tools/list` probe below — carry no `params.name`, so they never match and are
denied.)

---

## Cleanup

Undo everything, in reverse order:

```bash
# 1. delete the inbound policy CR
kubectl delete -f aiac/docs/examples/opa-team1-policy.yaml

# 2. revert authproxy-routes to weather-only
kubectl patch configmap authproxy-routes -n team1 --type merge -p "$(python3 -c '
import json
print(json.dumps({"data":{"routes.yaml":
"""- host: \"weather-tool-advanced-mcp\"
  target_audience: \"spiffe://localtest.me/ns/team1/sa/weather-tool-advanced\"
  token_scopes: \"openid weather-tool-exchange-aud\"
"""}}))')"

# 3. remove the temporary optional client scope from github-agent
KC=http://keycloak.localtest.me:8080
ADMIN=$(curl -s -X POST "$KC/realms/master/protocol/openid-connect/token" \
  -d client_id=admin-cli -d username=admin -d password=admin -d grant_type=password \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
CID=$(curl -s -H "Authorization: Bearer $ADMIN" "$KC/admin/realms/rossoctl/clients" \
  | python3 -c 'import sys,json;print(next(c["id"] for c in json.load(sys.stdin) if c["clientId"].endswith("/sa/github-agent")))')
SID=$(curl -s -H "Authorization: Bearer $ADMIN" "$KC/admin/realms/rossoctl/client-scopes" \
  | python3 -c 'import sys,json;print(next(s["id"] for s in json.load(sys.stdin) if s["name"]=="agent-team1-github-tool-aud"))')
curl -s -o /dev/null -w "remove scope HTTP %{http_code}\n" -X DELETE -H "Authorization: Bearer $ADMIN" \
  "$KC/admin/realms/rossoctl/clients/$CID/optional-client-scopes/$SID"

# 4. revert the pipeline (removes the OPA overlay, restarts the agents)
ROSSOCTL_DIR=../rossoctl ./scripts/opa-kind-restore.sh
```

Confirm OPA is gone from the pipeline (expect **0**):

```bash
kubectl get configmap authbridge-runtime-config -n team1 \
  -o jsonpath='{.data.config\.yaml}' | grep -c 'name: opa'
# 0
```

The Keycloak realm/user changes from the Prerequisites are shared, cluster-wide
state and are harmless to leave in place for future runs.
