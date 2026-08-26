#!/usr/bin/env bash
# sidecar-patch.sh — attach the lineage sidecar to an EXISTING Deployment.
#
# attach-lineage.sh owns the deploy-it-yourself path: it EMITS a complete
# Deployment (app re-wrapped with the OTel shim + sidecar). This script is the
# complement for apps you do NOT deploy yourself — operator-managed or
# UI-imported workloads — where the Deployment already exists and must keep its
# owner's spec. It only ADDS the sidecar pieces via a strategic-merge
# patch (lists merge by name: the app container is untouched; interception is
# transparent iptables — no HTTP_PROXY, no code change, no image change).
#
# Every YAML byte comes from attach-lineage.sh, the ONE generator (EMIT=cm for
# the per-app plugin ConfigMap, EMIT=patch for the sidecar patch) — this script
# only applies them and waits. CAVEAT the owner keeps owning the object: a
# platform rewrite of the Deployment silently drops the patch (observed live
# when an operator reconciled a patched Deployment) — re-run this script after
# any platform-side change.
#
# The target must NOT already carry a platform-injected AuthBridge sidecar
# (a Deployment enrolled through an AgentRuntime CR / `<prefix>/inject:
# enabled` has one): the patch would add a second sidecar on the same ports.
# Nor may the app itself listen on 9090, 15123 or 15124 — the sidecar binds
# those in the shared pod network namespace. Both are checked below.
#
# NOTE: natively-instrumented apps need no shim — in-process context already
# propagates. Apps that are NOT instrumented still need the shim for correct
# pairing under concurrency; for those, use attach-lineage.sh (EMIT=manifest),
# which deploys the app re-wrapped with the shim. See README.md "Which path".
#
# Usage (env-driven, like attach-lineage.sh):
#   DEPLOY=echo-upstream ./sidecar-patch.sh
#
# Env:
#   DEPLOY                  target Deployment name (required)
#   NAMESPACE               default team1
#   SELF_ID                 lineage self_id (default: $DEPLOY)
#   OTEL_ENDPOINT           OTLP/gRPC endpoint the lineage plugin exports to
#                           (default otel-collector.rossoctl-system.svc.cluster.local:4317).
#   SIDECAR_IMAGE           sidecar image override — read by attach-lineage.sh
#   PROXY_INIT_IMAGE        from the environment this script inherits (set them
#                           on the command line like DEPLOY; see its header).
#   OUTBOUND_PORTS_EXCLUDE  comma-separated ports proxy-init must NOT intercept
#                           (inherited the same way). An app that exports its
#                           own OTLP telemetry should have that export port
#                           excluded, so its telemetry keeps flowing untouched.
#                           LLM / MCP ports are deliberately NOT excluded —
#                           those are the hops lineage exists to observe.
#
# Requires in the target namespace: the platform-rendered `envoy-config`
# ConfigMap, and the sidecar + proxy-init images resolvable from the cluster
# (see README.md "Prerequisites").
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

DEPLOY="${DEPLOY:?usage: DEPLOY=<deployment> [NAMESPACE=team1] [SELF_ID=<id>] [OUTBOUND_PORTS_EXCLUDE=ports] sidecar-patch.sh}"
NAMESPACE="${NAMESPACE:-team1}"
SELF_ID="${SELF_ID:-$DEPLOY}"

kubectl get deploy -n "$NAMESPACE" "$DEPLOY" >/dev/null
kubectl get cm -n "$NAMESPACE" envoy-config >/dev/null || {
  echo "error: ConfigMap envoy-config missing in $NAMESPACE (rendered by the platform chart)" >&2
  exit 1
}
# Refuse a target whose pod already binds a port the sidecar needs: either an
# injected AuthBridge sidecar is present (15123/15124/9090 all taken — the
# patch would add a SECOND one and the pod would crash-loop with nothing
# pointing back here), or the app itself listens on one of them. Detected by
# declared container ports rather than a container name, which the operator
# owns. An app port that is not declared cannot be seen from here.
declared_ports="$(kubectl get deploy -n "$NAMESPACE" "$DEPLOY" \
  -o jsonpath='{range .spec.template.spec.containers[*].ports[*]}{.containerPort}{" "}{end}')"
for p in 15124 15123 9090; do
  case " $declared_ports " in
    *" $p "*)
      echo "error: $DEPLOY already declares containerPort $p, which the lineage sidecar binds" >&2
      echo "  (an operator-injected sidecar, or the app itself on that port) — refusing to add a second binder" >&2
      exit 1 ;;
  esac
done

# This script attaches capture only. Whether the app carries trace context
# from its inbound request to its outbound calls is a property of the app
# (its own instrumentation, or the shim via attach-lineage.sh) that nothing
# here can see or change — say so once, at attach time, instead of guessing
# from the Deployment's env.
echo "NOTE: the sidecar records every hop; whether $DEPLOY's outbound hops attribute to" >&2
echo "      their inbound depends on the app propagating traceparent itself. Verify" >&2
echo "      pairing under concurrency before relying on it (README.md, 'The envelope')." >&2

gen() {  # $1 = EMIT mode; forwards the shared knobs to the one generator
  EMIT="$1" NAME="$DEPLOY" SELF_ID="$SELF_ID" NAMESPACE="$NAMESPACE" \
    "${SCRIPT_DIR}/attach-lineage.sh"
}

gen cm | kubectl apply -f -
kubectl patch deploy "$DEPLOY" -n "$NAMESPACE" --type strategic --patch "$(gen patch)"

kubectl rollout status -n "$NAMESPACE" "deploy/$DEPLOY" --timeout=180s
echo ">> lineage sidecar attached to deploy/$DEPLOY (self_id=$SELF_ID, ns=$NAMESPACE)"
