#!/usr/bin/env bash
# lineage-switch.sh — THE general lineage switch. One command turns the entire
# two-span lineage pipeline on or off:
#
#   ./lineage-switch.sh off     # 1. fleet apps redeployed BARE: no OTel shim
#                               #    wrapper AND no lineage-telemetry plugin in
#                               #    the sidecar (NO_PROPAGATE=1 NO_EMIT=1
#                               #    through deploy-fleet.sh, manifests only —
#                               #    no image builds)
#                               # 2. data-governance stack scaled to 0: no
#                               #    receiver, no UI, no interactions processor,
#                               #    no postgres POD (the PVC — the data —
#                               #    survives untouched)
#   ./lineage-switch.sh on      # the reverse: fleet re-instrumented, DG stack
#                               #    scaled back up (receiver=2, others=1)
#   ./lineage-switch.sh status  # per-app shim/plugin state + DG replica counts
#
# What this does NOT touch:
#   - otel-collector (kagenti-system): shared infra (Phoenix). With lineage off
#     its DG exporter logs connection errors for any spans that still arrive
#     from self-instrumenting stock apps (e.g. operator-managed weather-service)
#     — harmless noise, nothing is stored.
#   - operator-managed agents (not in fleet.yaml): they were never adapted.
#   - Postgres data: scaling the StatefulSet to 0 keeps the PVC; "on" resumes
#     with all history present.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
NAMESPACE="${NAMESPACE:-team1}"
DG_NS="${DG_NS:-data-governance}"

usage() { echo "usage: $0 on|off|status" >&2; exit 2; }
[ $# -eq 1 ] || usage

fleet_names() {  # app names from the validated catalog
  python3 "${SCRIPT_DIR}/fleet-read.py" --names "${SCRIPT_DIR}/fleet.yaml"
}

case "$1" in
  off)
    echo ">> [1/2] redeploying fleet bare (no shim, no lineage plugin)"
    NO_PROPAGATE=1 NO_EMIT=1 SKIP_BUILD=1 "${SCRIPT_DIR}/deploy-fleet.sh"
    echo ">> [2/2] scaling data-governance stack to 0 (PVC/data preserved)"
    kubectl scale -n "$DG_NS" deploy --all --replicas=0
    kubectl scale -n "$DG_NS" statefulset data-governance-postgres --replicas=0
    echo ">> lineage is OFF"
    ;;
  on)
    echo ">> [1/2] scaling data-governance stack up"
    kubectl scale -n "$DG_NS" statefulset data-governance-postgres --replicas=1
    kubectl scale -n "$DG_NS" deploy data-governance-receiver --replicas=2
    kubectl scale -n "$DG_NS" deploy data-governance-ui data-governance-interactions --replicas=1
    # classification is optional (not deployed on every cluster) — restore it
    # only where it exists; `off` scales it down via --all either way.
    if kubectl get deploy -n "$DG_NS" data-governance-classification >/dev/null 2>&1; then
      kubectl scale -n "$DG_NS" deploy data-governance-classification --replicas=1
    fi
    kubectl rollout status -n "$DG_NS" statefulset/data-governance-postgres --timeout=120s
    echo ">> [2/2] redeploying fleet instrumented (shim + lineage plugin)"
    SKIP_BUILD=1 "${SCRIPT_DIR}/deploy-fleet.sh"
    echo ">> lineage is ON"
    ;;
  status)
    printf '%-24s %-6s %-8s\n' APP SHIM PLUGIN
    for name in $(fleet_names); do
      cmd="$(kubectl get deploy -n "$NAMESPACE" "$name" \
        -o jsonpath='{.spec.template.spec.containers[0].command}' 2>/dev/null || echo ABSENT)"
      case "$cmd" in
        ABSENT) shim="-" ;;
        *opentelemetry-instrument*) shim="on" ;;
        *) shim="off" ;;
      esac
      cm="$(kubectl get cm -n "$NAMESPACE" "authbridge-lineage-config-${name}" \
        -o jsonpath='{.data.config\.yaml}' 2>/dev/null || echo ABSENT)"
      case "$cm" in
        ABSENT) plugin="-" ;;
        *lineage-telemetry*) plugin="on" ;;
        *) plugin="off" ;;
      esac
      printf '%-24s %-6s %-8s\n' "$name" "$shim" "$plugin"
    done
    echo
    echo "data-governance replicas:"
    kubectl get -n "$DG_NS" deploy,statefulset \
      -o custom-columns='NAME:.metadata.name,REPLICAS:.spec.replicas'
    ;;
  *) usage ;;
esac
