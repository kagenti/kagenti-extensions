#!/usr/bin/env bash
# sidecar-patch.sh — attach the lineage sidecar to an EXISTING Deployment.
#
# attach-lineage.sh owns the fleet path: it EMITS a complete Deployment (app
# re-wrapped with the OTel shim + sidecar). This script is the complement for
# apps we do NOT deploy ourselves — operator-managed / UI-imported apps such as
# the stock weather pair — where the Deployment already exists and must keep
# its operator-set spec. It only ADDS the sidecar pieces via a strategic-merge
# patch (lists merge by name: the app container is untouched; interception is
# transparent iptables — no HTTP_PROXY, no code change, no image change).
#
# Every YAML byte comes from attach-lineage.sh, the ONE generator (EMIT=cm for
# the per-app plugin ConfigMap, EMIT=patch for the sidecar patch) — this script
# only applies them and waits. CAVEAT the owner keeps owning the object: a
# platform rewrite of the Deployment silently drops the patch (observed live on
# weather-tool, 2026-08-11) — re-run this script after any platform-side change.
#
# NOTE: natively-instrumented apps (weather_service/weather_tool export their
# own OTLP) need no shim — in-process context already propagates. Apps that are
# NOT instrumented still need the shim for correct pairing under concurrency;
# for those, prefer a fleet row + deploy-fleet.sh (see RUNBOOK.md).
#
# Usage (env-driven, like attach-lineage.sh):
#   DEPLOY=weather-service OUTBOUND_PORTS_EXCLUDE=8335 ./sidecar-patch.sh
#
# Env:
#   DEPLOY                  target Deployment name (required)
#   NAMESPACE               default team1
#   SELF_ID                 lineage self_id (default: $DEPLOY)
#   OTEL_ENDPOINT           collector the lineage plugin exports to (default
#                           otel-collector.kagenti-system.svc.cluster.local:4317;
#                           rossoctl-branded platforms: otel-collector.rossoctl-system...).
#   OUTBOUND_PORTS_EXCLUDE  comma-separated ports proxy-init must NOT intercept.
#                           Natively-instrumented kagenti apps export their own
#                           OTLP to the collector on :8335 — exclude it so that
#                           app telemetry keeps flowing untouched. Ollama/MCP
#                           ports are deliberately NOT excluded (we WANT lineage
#                           to observe those hops).
#
# Requires in the target namespace: the platform-rendered `envoy-config`
# ConfigMap, and the docker.io/library/{authbridge-envoy,proxy-init}:latest
# images loaded into the cluster (RUNBOOK.md "Build the sidecar images").
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

DEPLOY="${DEPLOY:?usage: DEPLOY=<deployment> [NAMESPACE=team1] [SELF_ID=<id>] [OUTBOUND_PORTS_EXCLUDE=ports] sidecar-patch.sh}"
NAMESPACE="${NAMESPACE:-team1}"
SELF_ID="${SELF_ID:-$DEPLOY}"

kubectl get deploy -n "$NAMESPACE" "$DEPLOY" >/dev/null
kubectl get cm -n "$NAMESPACE" envoy-config >/dev/null || {
  echo "error: ConfigMap envoy-config missing in $NAMESPACE (platform-rendered by the kagenti chart)" >&2
  exit 1
}

# Adopt targets are natively-instrumented apps (no shim) — they only propagate
# trace context when their own OTel SDK is configured. A Deployment without
# OTEL_EXPORTER_OTLP_ENDPOINT (seen with UI-"Deploy From Image" imports, which
# drop the example manifests' env) records exchanges that scatter into orphan
# traces: sidecar attaches fine, forests never link. Warn at attach time.
if ! kubectl get deploy -n "$NAMESPACE" "$DEPLOY" \
    -o jsonpath='{.spec.template.spec.containers[*].env[*].name}' \
    | grep -q OTEL_EXPORTER_OTLP_ENDPOINT; then
  echo "WARNING: deploy/$DEPLOY has no OTEL_EXPORTER_OTLP_ENDPOINT env." >&2
  echo "  Its app will not propagate trace context; its exchanges will land in" >&2
  echo "  orphan traces instead of the caller's tree. If the app is OTel-capable:" >&2
  echo "    kubectl set env -n $NAMESPACE deploy/$DEPLOY \\" >&2
  echo "      OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector.<platform-ns>.svc.cluster.local:8335" >&2
  echo "  (If it is NOT OTel-instrumented, use the fleet path — it adds the shim.)" >&2
fi

gen() {  # $1 = EMIT mode; forwards the shared knobs to the one generator
  EMIT="$1" NAME="$DEPLOY" SELF_ID="$SELF_ID" NAMESPACE="$NAMESPACE" \
    "${SCRIPT_DIR}/attach-lineage.sh"
}

gen cm | kubectl apply -f -
kubectl patch deploy "$DEPLOY" -n "$NAMESPACE" --type strategic --patch "$(gen patch)"

kubectl rollout status -n "$NAMESPACE" "deploy/$DEPLOY" --timeout=180s
echo ">> lineage sidecar attached to deploy/$DEPLOY (self_id=$SELF_ID, ns=$NAMESPACE)"
