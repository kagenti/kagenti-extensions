#!/usr/bin/env bash
# opa-kind-restore.sh — revert opa-kind-enable.sh.
#
# Re-applies the rossoctl chart's real, untouched charts/rossoctl/values.yaml
# (no OPA/parser overlay) and restarts the authbridge sidecars so they pick
# up the reverted pipeline. Mirrors the "Rollback" section of
# authbridge/docs/opa-kind-runbook.md verbatim — since opa-kind-enable.sh
# never wrote to values.yaml, "restoring" it is just re-running helm upgrade
# against that same file with no overlay on top.
#
# Drops the pipeline overlay (-f OVERLAY_FILE) but keeps the same
# cluster-shape --set flags as the enable step (openshift, featureFlags.
# agentSandbox, the local image override) — those describe the Kind
# cluster/local-image setup, not the OPA overlay, and the chart's own
# defaults assume OpenShift (openshift: true), so dropping them breaks
# the upgrade on Kind (see mcp-gateway.yaml's openshiftDomain check).
#
# Env vars:
#   ROSSOCTL_DIR        path to the rossoctl/rossoctl repo clone (the chart)
#   RELEASE_NAME        helm release name                 (default: rossoctl)
#   RELEASE_NAMESPACE   namespace the chart is installed in (default: rossoctl-system)
#   AGENT_NAMESPACE     namespace to restart agent pods in (default: team1)
#   IMAGE_TAG           local authbridge-proxy image tag  (default: localhost/authbridge:local)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CORTEX_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

ROSSOCTL_DIR="${ROSSOCTL_DIR:-$(cd "$CORTEX_DIR/../rossoctl" 2>/dev/null && pwd || echo "")}"
RELEASE_NAME="${RELEASE_NAME:-rossoctl}"
RELEASE_NAMESPACE="${RELEASE_NAMESPACE:-rossoctl-system}"
AGENT_NAMESPACE="${AGENT_NAMESPACE:-team1}"
IMAGE_TAG="${IMAGE_TAG:-localhost/authbridge:local}"

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

echo "==> Restoring original pipeline from ${VALUES_FILE} (no OPA/parser overlay)"
( cd "$CHART_DIR" && helm dependency build )
helm upgrade "$RELEASE_NAME" "$CHART_DIR" -n "$RELEASE_NAMESPACE" \
  -f "$VALUES_FILE" \
  --set openshift=false \
  --set featureFlags.agentSandbox=true \
  --set operator-chart.defaults.images.authbridge="$IMAGE_TAG"

echo "==> Restarting authbridge pods in ${AGENT_NAMESPACE}"
kubectl delete pods -n "$AGENT_NAMESPACE" -l rossoctl.io/type=agent

cat <<EOF
==> Done.

Verify the pipeline is back to its original state (count depends on what
values.yaml originally shipped — 0 if it never had OPA):
  kubectl get configmap authbridge-runtime-config -n ${AGENT_NAMESPACE} \\
    -o jsonpath='{.data.config\.yaml}' | grep -c 'name: opa'
EOF
