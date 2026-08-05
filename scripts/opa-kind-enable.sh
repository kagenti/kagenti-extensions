#!/usr/bin/env bash
# opa-kind-enable.sh — authbridge/docs/opa-kind-runbook.md Steps 1-5, on the fly.
#
# Wires the OPA plugin into every agent's inbound AND outbound AuthBridge
# pipeline on a Kind cluster, alongside the full parser set (a2a-parser,
# mcp-parser, inference-parser) so OPA policies have input.a2a / input.mcp /
# input.inference available on both legs, not just input.host.
#
# On the outbound leg OPA is placed AFTER token-exchange so policies can
# read input.delegation (the target audience + scopes the agent's token was
# exchanged for). See the overlay comment in Step 3 for the rationale.
#
# Does NOT modify charts/rossoctl/values.yaml on disk. The pipeline override
# lives in a throwaway temp file merged on top of the real values.yaml via a
# second `helm upgrade -f` — Helm layers -f files left-to-right, so the repo
# file is only ever read, never written. Run opa-kind-restore.sh to revert.
#
# Requires: kubectl, helm, kind, docker (or podman), python3 not needed here.
# Env vars:
#   OPERATOR_DIR        path to the rossoctl/operator repo clone (bundle-service)
#   ROSSOCTL_DIR        path to the rossoctl/rossoctl repo clone (the chart)
#   CLUSTER_NAME        kind cluster name                 (default: rossoctl)
#   RELEASE_NAME        helm release name                 (default: rossoctl)
#   RELEASE_NAMESPACE   namespace the chart is installed in (default: rossoctl-system)
#   AGENT_NAMESPACE     namespace to restart agent pods in (default: team1)
#   IMAGE_TAG           local authbridge-proxy image tag  (default: localhost/authbridge:local)
#   CONTAINER_RUNTIME   docker | podman                   (default: docker, auto-falls back to podman)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CORTEX_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

OPERATOR_DIR="${OPERATOR_DIR:-$(cd "$CORTEX_DIR/../operator" 2>/dev/null && pwd || echo "")}"
ROSSOCTL_DIR="${ROSSOCTL_DIR:-$(cd "$CORTEX_DIR/../rossoctl" 2>/dev/null && pwd || echo "")}"
CLUSTER_NAME="${CLUSTER_NAME:-rossoctl}"
RELEASE_NAME="${RELEASE_NAME:-rossoctl}"
RELEASE_NAMESPACE="${RELEASE_NAMESPACE:-rossoctl-system}"
AGENT_NAMESPACE="${AGENT_NAMESPACE:-team1}"
IMAGE_TAG="${IMAGE_TAG:-localhost/authbridge:local}"

if [ -z "$OPERATOR_DIR" ] || [ ! -d "$OPERATOR_DIR" ]; then
  echo "ERROR: Set OPERATOR_DIR to point to your rossoctl/operator repo clone" >&2
  exit 1
fi
if [ -z "$ROSSOCTL_DIR" ] || [ ! -d "$ROSSOCTL_DIR" ]; then
  echo "ERROR: Set ROSSOCTL_DIR to point to your rossoctl/rossoctl repo clone" >&2
  exit 1
fi

VALUES_FILE="${ROSSOCTL_DIR}/charts/rossoctl/values.yaml"
CHART_DIR="${ROSSOCTL_DIR}/charts/rossoctl"
if [ ! -f "$VALUES_FILE" ]; then
  echo "ERROR: ${VALUES_FILE} not found — check ROSSOCTL_DIR" >&2
  exit 1
fi

if [ "${KIND_EXPERIMENTAL_PROVIDER:-}" = "podman" ]; then
  CONTAINER_RUNTIME="${CONTAINER_RUNTIME:-podman}"
elif ! command -v docker &> /dev/null && command -v podman &> /dev/null; then
  CONTAINER_RUNTIME="${CONTAINER_RUNTIME:-podman}"
else
  CONTAINER_RUNTIME="${CONTAINER_RUNTIME:-docker}"
fi

# Track every temp file we create and remove them on exit, so an early failure
# (set -e) under any step still cleans up. Trailing-X templates only (no suffix
# after the Xs) for portability across GNU and BSD/macOS mktemp.
TMPFILES=()
cleanup() { [ "${#TMPFILES[@]}" -gt 0 ] && rm -f "${TMPFILES[@]}"; }
trap cleanup EXIT

load_image_to_kind() {
  local image_name="$1"
  if [ "$CONTAINER_RUNTIME" = "podman" ]; then
    local tar_file
    tar_file="$(mktemp "${TMPDIR:-/tmp}/opa-kind-enable-image.XXXXXX")"
    TMPFILES+=("$tar_file")
    "$CONTAINER_RUNTIME" save "$image_name" -o "$tar_file"
    kind load image-archive "$tar_file" --name "$CLUSTER_NAME"
    rm -f "$tar_file"
  else
    kind load docker-image "$image_name" --name "$CLUSTER_NAME"
  fi
}

OVERLAY_FILE="$(mktemp "${TMPDIR:-/tmp}/opa-kind-enable-overlay.XXXXXX")"
TMPFILES+=("$OVERLAY_FILE")

echo "==> Step 1/5: deploying bundle-service (${OPERATOR_DIR})"
( cd "$OPERATOR_DIR" && ./operator/hack/bundle-service-kind.sh "$CLUSTER_NAME" "$RELEASE_NAMESPACE" )
kubectl get pods -n "$RELEASE_NAMESPACE" -l app=bundle-service

echo "==> Step 2/5: building + loading authbridge-proxy (${IMAGE_TAG}) via ${CONTAINER_RUNTIME}"
( cd "$CORTEX_DIR/authbridge" && "$CONTAINER_RUNTIME" build -t "$IMAGE_TAG" -f cmd/authbridge-proxy/Dockerfile . )
load_image_to_kind "$IMAGE_TAG"

echo "==> Step 3/5: writing throwaway pipeline overlay (${VALUES_FILE} stays untouched)"
cat > "$OVERLAY_FILE" <<YAML
# Throwaway overlay — merged on top of the real values.yaml at helm-upgrade
# time, never written back to it. Adds OPA plus the full parser set
# (a2a-parser, mcp-parser, inference-parser) to both pipeline legs:
#   - Parsers run before jwt-validation/opa so their signals (input.a2a,
#     input.mcp, input.inference) are always populated, even if a later
#     gate denies the request — matches the a2a-parser convention in
#     authbridge/demos/ibac/k8s/ibac-patch.yaml.
#   - opa runs after jwt-validation on inbound so input.identity is set
#     (see authbridge/docs/opa-migration-guide.md Step 1).
#   - On OUTBOUND, opa runs AFTER token-exchange so the delegation signal
#     is populated: token-exchange records the target audience + granted
#     scopes it minted a token for (RFC 8693) into the delegation chain,
#     and OPA exposes it as input.delegation (origin, actor, depth, and a
#     chain of {subject_id, audience, scopes, strategy, from_cache}). This
#     lets outbound policy reason about WHAT the agent's token was
#     exchanged for — e.g. "deny github-full-access to non-admin agents" —
#     without re-parsing the minted token and without a fail-closed JWT
#     gate that would reject passthrough egress. input.identity stays
#     empty outbound (no JWT is validated on this leg); input.delegation is
#     the outbound identity signal.
#   - token-exchange uses the chart's default shape (client-secret identity
#     from /shared, passthrough default policy). Per-destination routes
#     come from the authproxy-routes ConfigMap; hosts with no route fall
#     through unchanged and simply carry no delegation hop.
# NOTE: the rossoctl chart reads the pipeline from `.Values.authBridge.pipeline`
# (a multiline string rendered via tpl() into the namespace
# authbridge-runtime-config ConfigMap — see charts/rossoctl/templates/
# _helpers.tpl "rossoctl.authbridge-runtime-config-yaml"). The operator webhook
# then uses that ConfigMap's `pipeline:` verbatim as the base for each per-agent
# authbridge-config-<agent> ConfigMap. So the override MUST be nested under
# `authBridge.pipeline` — a top-level `pipeline:` key is silently ignored.
authBridge:
  pipeline: |
    inbound:
      plugins:
        - name: a2a-parser
        - name: mcp-parser
        - name: inference-parser
        - name: jwt-validation
          config:
            issuer: "http://keycloak.localtest.me:8080/realms/rossoctl"
            keycloak_url: "http://keycloak-service.keycloak.svc:8080"
            keycloak_realm: "rossoctl"
        - name: opa
          config:
            bundle_url: "http://bundle-service.rossoctl-system.svc.cluster.local:8080"
    outbound:
      plugins:
        - name: a2a-parser
        - name: mcp-parser
        - name: inference-parser
        - name: token-exchange
          config:
            keycloak_url: "http://keycloak-service.keycloak.svc:8080"
            keycloak_realm: "rossoctl"
            default_policy: "passthrough"
            identity:
              type: "client-secret"
        - name: opa
          config:
            bundle_url: "http://bundle-service.rossoctl-system.svc.cluster.local:8080"
YAML

echo "==> Step 4/5: helm upgrade (base values.yaml + overlay — base file not modified)"
( cd "$CHART_DIR" && helm dependency build )
helm upgrade "$RELEASE_NAME" "$CHART_DIR" -n "$RELEASE_NAMESPACE" \
  -f "$VALUES_FILE" \
  -f "$OVERLAY_FILE" \
  --set openshift=false \
  --set featureFlags.agentSandbox=true \
  --set operator-chart.defaults.images.authbridge="$IMAGE_TAG" \
  --wait --timeout 5m

echo "==> Step 5/5: restarting authbridge pods in ${AGENT_NAMESPACE}"
# --ignore-not-found so this no-ops cleanly when the namespace has no agent pods yet.
kubectl delete pods -n "$AGENT_NAMESPACE" -l rossoctl.io/type=agent --ignore-not-found

cat <<EOF
==> Done.

Verify OPA + parsers are wired into both legs (expect 2 'name: opa' matches):
  kubectl get configmap authbridge-runtime-config -n ${AGENT_NAMESPACE} \\
    -o jsonpath='{.data.config\.yaml}' | grep -c 'name: opa'

Restore the original pipeline with:
  ./scripts/opa-kind-restore.sh
EOF
