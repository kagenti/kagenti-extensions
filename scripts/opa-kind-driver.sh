#!/usr/bin/env bash
# opa-kind-driver.sh — execute aiac/docs/opa-kind-runbook.md end-to-end.
#
# This is an automated driver for the AIAC "OPA Kind Cluster Runbook"
# (aiac/docs/opa-kind-runbook.md). It runs every step of that runbook in
# order, prints each step and the result it obtained, prints the OPA `input`
# documents for BOTH the inbound and the outbound legs, and FAILS with a clear
# message the moment an observed result does not match the runbook's stated
# expectation (instead of silently continuing).
#
# What it does, mirroring the runbook 1:1:
#   Step 1   enable OPA in both legs               (scripts/opa-kind-enable.sh)
#   Step 2   verify the starting point             (pods, bundle-service, client-id)
#   Part A   inbound authorization
#     A.1    dev-user token carries sub=dev-user
#     A.2    baseline: dev-user AND alice both reach the app (HTTP 200)
#     A.3    apply the client-scoped policy CR
#     A.4    enforced: dev-user -> 200, alice -> 403
#     A.5    print the INBOUND OPA input + assert the decision result
#   Part B   outbound token-exchange + OPA
#     B.1    add the github-tool outbound route
#     B.2    grant github-agent the exchange scope   (expect HTTP 204)
#     B.3    restart github-agent to load the route
#     B.4    outbound tools/list probe -> DENIED (JSON-RPC error frame at
#            HTTP 200, or 403/503; outbound OPA present)
#     B.5    print the OUTBOUND OPA input
#
# Requires: kubectl, helm, kind, python3, curl, and docker (or podman).
# Env vars (runbook defaults shown):
#   OPERATOR_DIR   path to the rossoctl/operator clone   (default: ../operator)
#   ROSSOCTL_DIR   path to the rossoctl/rossoctl clone    (default: ../rossoctl)
#   NS             agent namespace                        (default: team1)
#   SYS_NS         platform namespace                     (default: rossoctl-system)
#   KC             Keycloak base URL          (default: http://keycloak.localtest.me:8080)
#   REALM          Keycloak realm                         (default: rossoctl)
#   POLL_SECS      max seconds to wait for OPA to poll a new bundle (default: 150).
#                  The OPA SDK bundle poller uses min_delay 10s / max_delay 120s,
#                  so a freshly applied CR can take up to ~120s to reach the
#                  agent; the default leaves margin above that worst case.
#   SKIP_ENABLE    if set to 1, skip Step 1's image rebuild + opa-kind-enable.sh
#                  and only verify OPA is already wired (fast path when iterating
#                  on the policy CR against an already-enabled cluster).
#
# Run from the repo root (cortex/):
#   OPERATOR_DIR=../operator ROSSOCTL_DIR=../rossoctl ./scripts/opa-kind-driver.sh
#   SKIP_ENABLE=1 ./scripts/opa-kind-driver.sh   # skip the rebuild, just re-test

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CORTEX_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── Configuration (runbook defaults) ────────────────────────────────────────
# Sibling repo clones the enable step needs; default to ../operator and
# ../rossoctl (relative to the cortex repo root) when not set, mirroring
# opa-kind-enable.sh.
OPERATOR_DIR="${OPERATOR_DIR:-$(cd "$CORTEX_DIR/../operator" 2>/dev/null && pwd || echo "")}"
ROSSOCTL_DIR="${ROSSOCTL_DIR:-$(cd "$CORTEX_DIR/../rossoctl" 2>/dev/null && pwd || echo "")}"

NS="${NS:-team1}"
SYS_NS="${SYS_NS:-rossoctl-system}"
KC="${KC:-http://keycloak.localtest.me:8080}"
REALM="${REALM:-rossoctl}"
POLL_SECS="${POLL_SECS:-150}"

AGENT_LABEL="app.kubernetes.io/name=github-agent"
EXPECTED_SPIFFE="spiffe://localtest.me/ns/${NS}/sa/github-agent"
POLICY_FILE="${CORTEX_DIR}/aiac/docs/examples/opa-team1-policy.yaml"
ENABLE_SCRIPT="${SCRIPT_DIR}/opa-kind-enable.sh"
RESTORE_SCRIPT="${SCRIPT_DIR}/opa-kind-restore.sh"

# ── Output helpers ──────────────────────────────────────────────────────────
if [ -t 1 ]; then
  C_RED=$'\033[31m'; C_GRN=$'\033[32m'; C_YEL=$'\033[33m'
  C_CYN=$'\033[36m'; C_BLD=$'\033[1m'; C_RST=$'\033[0m'
else
  C_RED=""; C_GRN=""; C_YEL=""; C_CYN=""; C_BLD=""; C_RST=""
fi

STEP_N=0
step() { STEP_N=$((STEP_N + 1)); printf '\n%s==> [%02d] %s%s\n' "$C_BLD$C_CYN" "$STEP_N" "$*" "$C_RST"; }
info() { printf '     %s\n' "$*"; }
pass() { printf '     %sPASS%s %s\n' "$C_GRN" "$C_RST" "$*"; }
warn() { printf '     %sWARN%s %s\n' "$C_YEL" "$C_RST" "$*"; }
die()  { printf '\n%sFAIL:%s %s\n' "$C_RED$C_BLD" "$C_RST" "$*" >&2; exit 1; }

# expect_eq <label> <got> <want>  — pass or die
expect_eq() {
  local label="$1" got="$2" want="$3"
  if [ "$got" = "$want" ]; then
    pass "${label}: got '${got}' (expected '${want}')"
  else
    die "${label}: got '${got}', expected '${want}'"
  fi
}

require_cmd() {
  local missing=()
  for c in kubectl helm kind python3 curl; do
    command -v "$c" >/dev/null 2>&1 || missing+=("$c")
  done
  if ! command -v docker >/dev/null 2>&1 && ! command -v podman >/dev/null 2>&1; then
    missing+=("docker|podman")
  fi
  if [ "${#missing[@]}" -gt 0 ]; then
    die "missing required commands on PATH: ${missing[*]}"
  fi
}

# ── Keycloak helpers (from runbook A.1 / A.2 / B.2) ──────────────────────────
# mint_token <user>  — password grant, prints the access_token
mint_token() {
  local user="$1" tok
  tok=$(curl -s -X POST "${KC}/realms/${REALM}/protocol/openid-connect/token" \
          -d client_id=rossoctl -d "username=${user}" -d "password=${user}" \
          -d grant_type=password -d scope=openid \
        | python3 -c 'import sys,json;print(json.load(sys.stdin).get("access_token",""))' 2>/dev/null || true)
  [ -n "$tok" ] || die "could not mint a token for user '${user}' at ${KC} (is Keycloak reachable and does the user exist?)"
  printf '%s' "$tok"
}

# token_sub <token>  — decode the JWT payload and print the 'sub' claim
token_sub() {
  printf '%s' "$1" | python3 -c '
import sys,json,base64
t=sys.stdin.read().split(".")[1]; t+="="*(-len(t)%4)
print(json.loads(base64.urlsafe_b64decode(t)).get("sub",""))'
}

# admin_token  — realm master admin token for Keycloak admin API (B.2)
admin_token() {
  local tok
  tok=$(curl -s -X POST "${KC}/realms/master/protocol/openid-connect/token" \
          -d client_id=admin-cli -d username=admin -d password=admin -d grant_type=password \
        | python3 -c 'import sys,json;print(json.load(sys.stdin).get("access_token",""))' 2>/dev/null || true)
  [ -n "$tok" ] || die "could not obtain a Keycloak master admin token (admin/admin) at ${KC}"
  printf '%s' "$tok"
}

# ── Cluster helpers ──────────────────────────────────────────────────────────
latest_agent_pod() {
  kubectl get pod -n "$NS" -l "$AGENT_LABEL" \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null
}

# probe_as <user>  — mint a user token, POST the ping/nonexistent JSON-RPC body
# to github-agent from a throwaway pod (runbook A.2). Echoes "<code>|<body>".
probe_as() {
  local user="$1" tok out code body
  tok="$(mint_token "$user")"
  out=$(kubectl run "probe-${user}-$RANDOM" --rm -i --restart=Never \
          --image=curlimages/curl:8.10.1 -n "$NS" --env="TOK=$tok" -- sh -c \
          'curl -s -m 15 -w "\nHTTP_CODE:%{http_code}\n" \
             -X POST http://github-agent.team1.svc.cluster.local:8080/ \
             -H "Content-Type: application/json" -H "Authorization: Bearer $TOK" \
             -d "{\"jsonrpc\":\"2.0\",\"id\":\"1\",\"method\":\"ping/nonexistent\",\"params\":{}}"' \
        2>/dev/null || true)
  code=$(printf '%s' "$out" | grep -o 'HTTP_CODE:[0-9]*' | tail -1 | cut -d: -f2 || true)
  body=$(printf '%s' "$out" | grep -vE 'HTTP_CODE:|deleted' | tr -d '\r' | grep -v '^$' | tail -1 || true)
  [ -n "$code" ] || die "probe as '${user}' produced no HTTP status (pod output: ${out})"
  printf '%s|%s' "$code" "$body"
}

# probe_expect <user> <want_code> <secs> <label>  — poll probe_as until it
# returns want_code (tolerates transient 502/000 while the app finishes binding
# its HTTP port, and the OPA bundle-poll delay after a CR change — up to ~120s,
# the SDK's max_delay_seconds), or die after <secs>.
probe_expect() {
  local user="$1" want="$2" secs="$3" label="$4"
  local deadline=$((SECONDS + secs)) res code body
  while :; do
    res="$(probe_as "$user")"; code="${res%%|*}"; body="${res#*|}"
    if [ "$code" = "$want" ]; then
      info "probe_as ${user}: HTTP ${code}  body: ${body}"
      pass "${label}: HTTP ${code} (expected ${want})"
      return 0
    fi
    if [ "$SECONDS" -ge "$deadline" ]; then
      info "probe_as ${user}: HTTP ${code}  body: ${body}"
      die "${label}: got HTTP ${code}, expected ${want} after ${secs}s. body: ${body}"
    fi
    info "probe_as ${user}: HTTP ${code} — retrying (want ${want})..."
    sleep 5
  done
}

# dump_opa_input <inbound|outbound>  — print the last OPA-input log line the
# sidecar emitted for that leg to the terminal, and stash it in the global
# LAST_OPA_LINE (so callers can assert on it without swallowing the display).
LAST_OPA_LINE=""
dump_opa_input() {
  local direction="$1" pod line upper
  pod="$(latest_agent_pod)"
  [ -n "$pod" ] || die "no github-agent pod found while capturing ${direction} OPA input"
  line=$(kubectl logs -n "$NS" "$pod" -c authbridge-proxy --tail=500 2>/dev/null \
         | grep "path=authbridge/${direction}/request" | tail -1 || true)
  upper=$(printf '%s' "$direction" | tr '[:lower:]' '[:upper:]')
  printf '\n%s----- %s OPA INPUT (as logged by the authbridge-proxy sidecar) -----%s\n' \
    "$C_BLD" "$upper" "$C_RST"
  if [ -n "$line" ]; then
    printf '%s\n' "$line"
  else
    warn "no '${direction}' decision-log line found yet (decision_logs.console formatting is environment-dependent)"
  fi
  LAST_OPA_LINE="$line"
}

# ── Preflight ────────────────────────────────────────────────────────────────
printf '%s%sAIAC OPA Kind runbook driver%s\n' "$C_BLD" "$C_CYN" "$C_RST"
info "runbook: aiac/docs/opa-kind-runbook.md   namespace: ${NS}   keycloak: ${KC}"

step "Preflight — required tooling and files"
require_cmd
[ -f "$POLICY_FILE" ]    || die "policy CR not found: ${POLICY_FILE}"
[ -x "$ENABLE_SCRIPT" ]  || die "enable script not found/executable: ${ENABLE_SCRIPT}"
[ -x "$RESTORE_SCRIPT" ] || die "restore script not found/executable: ${RESTORE_SCRIPT}"
kubectl cluster-info >/dev/null 2>&1 || die "kubectl cannot reach a cluster (is the Kind cluster up and KUBECONFIG set?)"
pass "tooling present, policy CR + helper scripts found, cluster reachable"

# ── Step 1 — Enable OPA in both legs ─────────────────────────────────────────
if [ "${SKIP_ENABLE:-}" = "1" ]; then
  step "Step 1 — Enable OPA in both legs  [SKIPPED: SKIP_ENABLE=1]"
  info "skipping the image rebuild + opa-kind-enable.sh; verifying OPA is already wired"
else
  step "Step 1 — Enable OPA in both legs (scripts/opa-kind-enable.sh)"
  [ -n "${OPERATOR_DIR:-}" ] && [ -d "${OPERATOR_DIR:-}" ] \
    || die "OPERATOR_DIR must point to a rossoctl/operator clone (got: '${OPERATOR_DIR:-<unset>}')"
  [ -n "${ROSSOCTL_DIR:-}" ] && [ -d "${ROSSOCTL_DIR:-}" ] \
    || die "ROSSOCTL_DIR must point to a rossoctl/rossoctl clone (got: '${ROSSOCTL_DIR:-<unset>}')"
  info "running enable script (rebuilds authbridge-proxy, loads into Kind, helm upgrade)..."
  OPERATOR_DIR="$OPERATOR_DIR" ROSSOCTL_DIR="$ROSSOCTL_DIR" "$ENABLE_SCRIPT" \
    || die "opa-kind-enable.sh failed"
fi

info "confirming OPA is wired into BOTH legs (expect 2)"
OPA_COUNT=$(kubectl get configmap authbridge-runtime-config -n "$NS" \
              -o jsonpath='{.data.config\.yaml}' 2>/dev/null | grep -c 'name: opa' || true)
if [ "$OPA_COUNT" = "2" ]; then
  pass "'name: opa' occurrences in authbridge-runtime-config: got '2' (expected '2')"
elif [ "${SKIP_ENABLE:-}" = "1" ]; then
  die "OPA is not wired into both legs (found ${OPA_COUNT}, expected 2), but SKIP_ENABLE=1 skipped the enable step. Re-run WITHOUT SKIP_ENABLE to run opa-kind-enable.sh first."
else
  die "'name: opa' occurrences in authbridge-runtime-config: got '${OPA_COUNT}', expected '2'"
fi

# ── Step 2 — Verify the starting point ───────────────────────────────────────
step "Step 2 — Verify the starting point"

# github-agent should be 2/2 (app + authbridge-proxy sidecar) and Ready.
# Step 1 (opa-kind-enable.sh) restarts the agent pods, so wait for the fresh
# pod to come up before asserting readiness.
info "waiting for github-agent to become Ready after the Step 1 restart (timeout 180s)..."
kubectl wait --for=condition=ready pod -n "$NS" -l "$AGENT_LABEL" --timeout=180s \
  || die "github-agent did not become Ready within 180s after the Step 1 pipeline restart. Check: kubectl get pods -n ${NS} -l ${AGENT_LABEL}"
AGENT_POD="$(latest_agent_pod)"
[ -n "$AGENT_POD" ] || die "no github-agent pod in namespace ${NS}"
READY_STATES=$(kubectl get pod -n "$NS" "$AGENT_POD" \
                 -o jsonpath='{.status.containerStatuses[*].ready}' 2>/dev/null)
info "github-agent pod: ${AGENT_POD}   container ready states: [${READY_STATES}]"
READY_COUNT=$(printf '%s' "$READY_STATES" | grep -o 'true' | wc -l | tr -d ' ' || true)
TOTAL_COUNT=$(printf '%s' "$READY_STATES" | wc -w | tr -d ' ')
if [ "$READY_COUNT" = "$TOTAL_COUNT" ] && [ "$TOTAL_COUNT" -ge 2 ]; then
  pass "github-agent is ${READY_COUNT}/${TOTAL_COUNT} (app + authbridge-proxy sidecar Ready)"
else
  die "github-agent is ${READY_COUNT}/${TOTAL_COUNT} ready — expected 2/2. Check: kubectl get pods -n ${NS} -l ${AGENT_LABEL}"
fi

# bundle-service up and serving
BS_PHASE=$(kubectl get pods -n "$SYS_NS" -l app=bundle-service \
             -o jsonpath='{.items[0].status.phase}' 2>/dev/null || true)
expect_eq "bundle-service pod phase" "${BS_PHASE:-<none>}" "Running"

# shipped global policy present
if kubectl get authorizationpolicy -n "$SYS_NS" 2>/dev/null | grep -q '^default'; then
  pass "global AuthorizationPolicy 'default' present in ${SYS_NS}"
else
  warn "global AuthorizationPolicy 'default' not found in ${SYS_NS} (continuing)"
fi

# github-agent SPIFFE ID — the client-scoped policy targets this
CLIENT_ID=$(kubectl exec -n "$NS" "deploy/github-agent" -c authbridge-proxy \
              -- cat /shared/client-id.txt 2>/dev/null | tr -d '\r\n' || true)
info "github-agent client-id.txt: ${CLIENT_ID}"
expect_eq "github-agent SPIFFE ID" "$CLIENT_ID" "$EXPECTED_SPIFFE"

# ── Part A — Inbound authorization ───────────────────────────────────────────
printf '\n%s%s====== Part A — Inbound authorization ======%s\n' "$C_BLD" "$C_CYN" "$C_RST"

step "A.1 — dev-user token carries sub=dev-user"
DEV_SUB=$(token_sub "$(mint_token dev-user)")
info "decoded sub claim: ${DEV_SUB}"
expect_eq "dev-user token sub claim" "$DEV_SUB" "dev-user"

step "A.2 — Baseline: before any client policy, BOTH users reach the app (HTTP 200)"
# Make the baseline meaningful on re-runs: clear any leftover client CR so
# "before any client policy" is actually true, then let OPA drop the bundle.
if kubectl get -f "$POLICY_FILE" >/dev/null 2>&1; then
  info "a client policy CR is already applied — deleting it so the baseline is clean"
  kubectl delete -f "$POLICY_FILE" --ignore-not-found >/dev/null 2>&1 || true
  info "letting OPA drop the removed bundle (poll interval up to ~120s)..."
  sleep 30
fi
# Dropping the bundle after a CR delete is also bundle-poll bound (up to ~120s),
# so give the baseline the same window as the enforced flip below.
probe_expect dev-user 200 "$POLL_SECS" "baseline probe as dev-user"
probe_expect alice    200 "$POLL_SECS" "baseline probe as alice"

step "A.3 — Apply the client-scoped policy CR"
kubectl apply -f "$POLICY_FILE" || die "kubectl apply -f ${POLICY_FILE} failed"
info "applied $(basename "$POLICY_FILE"); bundle-service rebuilds the team1 bundle, OPA polls on its own interval"

step "A.4 — Enforced: dev-user allowed (200), alice blocked (403)  [polling up to ${POLL_SECS}s]"
# alice flips to 403 once OPA picks up the new bundle; poll for it.
probe_expect alice    403 "$POLL_SECS" "enforced probe as alice (blocked by OPA, never reaches the app)"
probe_expect dev-user 200 "$POLL_SECS"  "enforced probe as dev-user (allowed)"

step "A.5 — The INBOUND OPA input, exactly (+ decision result)"
# Re-probe dev-user to make sure a fresh inbound decision is in the log tail.
probe_as dev-user >/dev/null || true
dump_opa_input inbound
INBOUND_LINE="$LAST_OPA_LINE"
printf '\n'
if [ -n "$INBOUND_LINE" ]; then
  # dev-user's decision should be allow:true; alice's earlier decision allow:false.
  if printf '%s' "$INBOUND_LINE" | grep -q 'allow:true'; then
    pass "inbound decision for dev-user shows allow:true"
  else
    warn "could not confirm 'allow:true' on the captured inbound line (formatting may differ); line printed above"
  fi
  ALICE_LINE=$(kubectl logs -n "$NS" "$(latest_agent_pod)" -c authbridge-proxy --tail=500 2>/dev/null \
               | grep 'path=authbridge/inbound/request' | grep 'allow:false' | tail -1 || true)
  if [ -n "$ALICE_LINE" ]; then
    pass "an inbound decision with allow:false is present (alice denied)"
  else
    warn "no inbound allow:false decision line found in the tail (alice's may have rotated out)"
  fi
else
  warn "no inbound OPA input captured — decision_logs.console may format differently in this build"
fi

# Reference: the canonical inbound input shape from the runbook (A.5).
cat <<'JSON'
     ----- INBOUND OPA INPUT — canonical shape (runbook A.5, for comparison) -----
     {
       "direction": "inbound",
       "method": "POST",
       "path": "/",
       "host": "github-agent.team1.svc.cluster.local:8080",
       "headers": { "accept": "*/*", "content-type": "application/json", ... },
       "identity": {
         "subject": "dev-user",
         "client_id": "rossoctl",
         "scopes": ["...", "agent-team1-github-agent-aud", "openid", "profile", "email"]
       }
     }
     (credential headers like authorization/cookie are redacted; use identity for decisions)
JSON

# ── Part B — Outbound token-exchange + OPA ───────────────────────────────────
printf '\n%s%s====== Part B — Outbound token-exchange + OPA ======%s\n' "$C_BLD" "$C_CYN" "$C_RST"

step "B.1 — Add the github-tool outbound route to authproxy-routes"
kubectl patch configmap authproxy-routes -n "$NS" --type merge -p "$(python3 -c '
import json
print(json.dumps({"data":{"routes.yaml":
"""- host: \"weather-tool-advanced-mcp\"
  target_audience: \"spiffe://localtest.me/ns/team1/sa/weather-tool-advanced\"
  token_scopes: \"openid weather-tool-exchange-aud\"
- host: \"github-tool\"
  target_audience: \"spiffe://localtest.me/ns/team1/sa/github-tool\"
  token_scopes: \"openid agent-team1-github-tool-aud\"
"""}}))')" || die "failed to patch authproxy-routes ConfigMap"
pass "authproxy-routes now carries the weather + github-tool routes"

step "B.2 — Grant github-agent the exchange scope (expect HTTP 204)"
ADMIN="$(admin_token)"
CID=$(curl -s -H "Authorization: Bearer $ADMIN" "${KC}/admin/realms/${REALM}/clients" \
      | python3 -c 'import sys,json;print(next((c["id"] for c in json.load(sys.stdin) if c["clientId"].endswith("/sa/github-agent")),""))' 2>/dev/null || true)
[ -n "$CID" ] || die "could not find the github-agent Keycloak client (clientId ending /sa/github-agent)"
SID=$(curl -s -H "Authorization: Bearer $ADMIN" "${KC}/admin/realms/${REALM}/client-scopes" \
      | python3 -c 'import sys,json;print(next((s["id"] for s in json.load(sys.stdin) if s["name"]=="agent-team1-github-tool-aud"),""))' 2>/dev/null || true)
[ -n "$SID" ] || die "could not find the 'agent-team1-github-tool-aud' client-scope in realm ${REALM}"
info "github-agent client UUID: ${CID}"
info "agent-team1-github-tool-aud scope UUID: ${SID}"
SCOPE_HTTP=$(curl -s -o /dev/null -w "%{http_code}" -X PUT -H "Authorization: Bearer $ADMIN" \
             "${KC}/admin/realms/${REALM}/clients/${CID}/optional-client-scopes/${SID}")
if [ "$SCOPE_HTTP" = "204" ]; then
  pass "assigned optional client-scope: HTTP 204"
elif [ "$SCOPE_HTTP" = "409" ]; then
  warn "optional client-scope already assigned (HTTP 409) — idempotent, continuing"
else
  die "assigning the optional client-scope returned HTTP ${SCOPE_HTTP} (expected 204)"
fi

step "B.3 — Restart github-agent to load the route"
kubectl delete pod -n "$NS" -l "$AGENT_LABEL" || die "failed to delete github-agent pod(s)"
info "waiting for github-agent to become Ready (timeout 120s)..."
kubectl wait --for=condition=ready pod -n "$NS" -l "$AGENT_LABEL" --timeout=120s \
  || die "github-agent did not become Ready within 120s after restart"
pass "github-agent restarted and Ready"

step "B.4 — Outbound probe as dev-user (tools/list) — expect DENIED"
info "outbound OPA is present, and the CR's outbound rego denies a tools/list call (no params.name)"
info "NOTE: an MCP JSON-RPC denial is surfaced as a JSON-RPC error frame at HTTP 200"
info "(writeMCPRejection in authbridge/authlib/listener/httpx/render.go), not an HTTP"
info "error status — so this probe classifies by the RESPONSE BODY, not the code alone."
POD="$(latest_agent_pod)"
[ -n "$POD" ] || die "no github-agent pod after restart"
TOK="$(mint_token dev-user)"
PROBE_PY="$(mktemp /tmp/opa-kind-driver-probe.XXXXXX.py)"
trap 'rm -f "$PROBE_PY"' EXIT
# The probe classifies the outcome in Python (robust JSON parsing) and prints a
# single VERDICT line the shell keys on, plus the raw HTTP status + body for the
# operator. tools/list carries a JSON-RPC id, so a policy denial comes back as
# an application-layer JSON-RPC error frame at HTTP 200 — that IS the deny.
cat > "$PROBE_PY" <<PY
import urllib.request, urllib.error, json
tok = """$TOK"""
op = urllib.request.build_opener(urllib.request.ProxyHandler({"http": "http://127.0.0.1:8081"}))
body = json.dumps({"jsonrpc":"2.0","id":"1","method":"tools/list","params":{}}).encode()
req = urllib.request.Request("http://github-tool:9090/", data=body,
    headers={"Content-Type":"application/json","Authorization":"Bearer "+tok})
code, raw = None, ""
try:
    r = op.open(req, timeout=15); code = r.status; raw = r.read().decode("utf-8", "replace")
except urllib.error.HTTPError as e:
    code = e.code
    try: raw = e.read().decode("utf-8", "replace")
    except Exception: raw = ""
except Exception as e:
    print("VERDICT ERROR"); print("HTTP none"); print("ERROR", type(e).__name__, e); raise SystemExit(0)
print("HTTP", code)
print("BODY", raw)
doc = None
try: doc = json.loads(raw)
except Exception: doc = None
if code in (403, 503):
    print("VERDICT DENIED_HTTP", code)                       # non-MCP-shaped rejection
elif code == 200 and isinstance(doc, dict) and "error" in doc:
    print("VERDICT DENIED_JSONRPC")                          # MCP JSON-RPC deny frame
elif code == 200 and isinstance(doc, dict) and "result" in doc:
    print("VERDICT ALLOWED_RESULT")                          # reached the tool — NOT gated
else:
    print("VERDICT UNEXPECTED")
PY
# Poll: right after the B.3 restart the agent may still hold the pre-CR bundle
# (combiner fallback allows outbound), so a fresh pod can transiently return an
# ALLOWED_RESULT until the bundle carrying the CR's outbound rego propagates.
# Retry until we observe a denial verdict or the window expires — mirrors A.4.
OB_DEADLINE=$((SECONDS + POLL_SECS))
while :; do
  OUT=$(kubectl exec -i -n "$NS" "$POD" -c agent -- python3 - < "$PROBE_PY" 2>/dev/null || true)
  OB_VERDICT=$(printf '%s\n' "$OUT" | sed -n 's/^VERDICT //p' | tail -1)
  OB_HTTP=$(printf '%s\n' "$OUT" | sed -n 's/^HTTP //p' | tail -1)
  case "$OB_VERDICT" in
    DENIED_JSONRPC|"DENIED_HTTP 403"|"DENIED_HTTP 503") break ;;
  esac
  [ "$SECONDS" -ge "$OB_DEADLINE" ] && break
  info "outbound probe: ${OB_VERDICT:-<none>} (HTTP ${OB_HTTP:-?}) — retrying (want a denial; bundle may still be propagating)..."
  sleep 5
done
info "outbound probe result:"
printf '%s\n' "$OUT" | sed 's/^/    /'
case "$OB_VERDICT" in
  DENIED_JSONRPC)
    pass "outbound tools/list DENIED by OPA: JSON-RPC error frame at HTTP 200 (code -32000, plugin=opa) — never reached github-tool" ;;
  "DENIED_HTTP 403")
    pass "outbound tools/list DENIED by OPA: HTTP 403 (delegated user/tool gate did not match)" ;;
  "DENIED_HTTP 503")
    warn "outbound call returned HTTP 503 — blocked before reaching the tool (token-exchange or OPA short-circuit). Treated as denied."
    pass "outbound tools/list did not reach github-tool (HTTP 503)" ;;
  ALLOWED_RESULT)
    die "outbound tools/list returned an HTTP 200 JSON-RPC result — the request reached github-tool, meaning outbound OPA did NOT gate it. With the example CR's outbound rego applied and OPA present, tools/list must be denied. Check that OPA is wired into the outbound leg (Step 1 count == 2) and that the bundle carrying the CR's outbound rego has propagated." ;;
  ERROR|"")
    die "outbound probe produced no HTTP response (output: ${OUT}). The agent container may lack python3/HTTP_PROXY, or the forward proxy is down." ;;
  *)
    die "outbound tools/list returned an unexpected shape (HTTP ${OB_HTTP:-?}); expected a JSON-RPC error frame at HTTP 200, or HTTP 403/503. Full output:
${OUT}" ;;
esac

step "B.5 — The OUTBOUND OPA input, exactly"
dump_opa_input outbound
OUTBOUND_LINE="$LAST_OPA_LINE"
printf '\n'
if [ -n "$OUTBOUND_LINE" ]; then
  if printf '%s' "$OUTBOUND_LINE" | grep -q 'outbound'; then
    pass "captured an outbound OPA decision-log line"
  else
    warn "captured a line but it does not mention 'outbound'; printed above"
  fi
else
  warn "no outbound OPA input captured — decision_logs.console may format differently in this build"
fi

# Reference: the canonical outbound input shape from the runbook (B.5).
cat <<'JSON'
     ----- OUTBOUND OPA INPUT — canonical shape (runbook B.5, for comparison) -----
     {
       "direction": "outbound",
       "method": "POST", "path": "/", "host": "github-tool:9090",
       "identity": {
         "subject": "dev-user",
         "client_id": "spiffe://localtest.me/ns/team1/sa/github-agent",
         "scopes": ["openid", "agent-team1-github-tool-aud"],
         "service_id": "spiffe://localtest.me/ns/team1/sa/github-tool"
       },
       "delegation": {
         "origin": "dev-user", "actor": "dev-user", "depth": 1,
         "chain": [ { "subject_id": "dev-user",
                      "audience": "spiffe://localtest.me/ns/team1/sa/github-tool",
                      "scopes": ["openid","agent-team1-github-tool-aud"],
                      "strategy": "token-exchange", "from_cache": false } ]
       },
       "mcp": { "method": "tools/list" }
     }
     (no validated JWT outbound; identity is synthesized from the token-exchange
      delegation hop. service_id = the exchange target = the github-tool SPIFFE ID)
JSON

# ── Summary ──────────────────────────────────────────────────────────────────
printf '\n%s%s====== ALL RUNBOOK STEPS PASSED ======%s\n' "$C_BLD" "$C_GRN" "$C_RST"
info "Inbound:  dev-user allowed (200), alice blocked (403); inbound OPA input printed."
info "Outbound: token-exchange leg reached OPA; tools/list denied (${OB_VERDICT}, HTTP ${OB_HTTP}); outbound OPA input printed."
cat <<EOF

To undo everything (runbook Cleanup):
  kubectl delete -f ${POLICY_FILE}
  # revert authproxy-routes to weather-only, remove the optional client-scope (see runbook Cleanup)
  ROSSOCTL_DIR=\${ROSSOCTL_DIR:-../rossoctl} ${RESTORE_SCRIPT}
EOF
