#!/usr/bin/env bash
# sidecar-patch.sh — attach the lineage sidecar to an EXISTING Deployment.
#
# The live applier. The Deployment is deployed and owned by someone else; this
# script only ADDS the lineage pieces through a strategic-merge patch (lists
# merge by name, so nothing the owner wrote changes; the app container is
# touched only when APP_CONTAINER opts it in). Every YAML byte comes from
# attach-lineage.sh: EMIT=cm (the plugin ConfigMap), EMIT=patch (the sidecar).
# This script checks, applies both, and waits for the rollout.
#
# Not durable: the owner keeps owning the object, and a platform rewrite (an
# operator reconcile, a UI redeploy) silently drops the patch — observed live
# when an operator reconciled a patched Deployment. Re-run after any
# platform-side change, or keep the attachment in your own manifests instead
# (README.md "Bring your own manifests"). To back out: `kubectl rollout undo`.
#
# Refused: a target that already carries an AuthBridge sidecar (the operator's
# is also a container named `envoy-proxy`, so the merge would silently take it
# over rather than sit beside it), or an app that itself listens on 9090,
# 15123 or 15124. Detected by declared container ports.
#
# Propagation: an uninstrumented app also needs the shim — bake it with
# build-otel-shim.sh, then pass APP_CONTAINER (+ APP_IMAGE) so the patch
# flips LINEAGE_PROPAGATE=1 on the app's own container. Without it this is
# capture only (README.md "The propagation half").
#
# Usage:
#   DEPLOY=echo-upstream ./sidecar-patch.sh
#   DEPLOY=my-agent APP_CONTAINER=agent \
#     APP_IMAGE=docker.io/library/my-agent-otel:latest ./sidecar-patch.sh
#
# Env — read here:
#   DEPLOY         target Deployment (required)
#   NAMESPACE      default team1
#   SELF_ID        lineage identity (default: DEPLOY)
#   APP_CONTAINER  the app container to switch propagation on (optional)
# Env — inherited by attach-lineage.sh and validated there (see its header):
#   APP_IMAGE, OTEL_ENDPOINT, SIDECAR_IMAGE, PROXY_INIT_IMAGE, NO_EMIT,
#   OUTBOUND_PORTS_EXCLUDE (an app's OWN telemetry port, or a plaintext non-HTTP store
#                   port such as Postgres/SMTP — never LLM/tool/S3 ports).
#
# Requires in the namespace: the platform's `envoy-config` ConfigMap; the
# sidecar + proxy-init images resolvable from the cluster (README "Prerequisites").
#
# Structure: read_inputs → preconditions (read-only; each returns or exits) →
# note_capture_only → apply (the only cluster writes). gen() is the one bridge
# to the generator.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

read_inputs() {
  DEPLOY="${DEPLOY:?usage: DEPLOY=<deployment> [NAMESPACE=team1] [SELF_ID=<id>] [APP_CONTAINER=<name> [APP_IMAGE=<ref>]] [SIDECAR_IMAGE=<ref> PROXY_INIT_IMAGE=<ref>] [OUTBOUND_PORTS_EXCLUDE=ports] sidecar-patch.sh}"
  NAMESPACE="${NAMESPACE:-team1}"
  SELF_ID="${SELF_ID:-$DEPLOY}"
  APP_CONTAINER="${APP_CONTAINER:-}"
}

require_deployment() {
  kubectl get deploy -n "$NAMESPACE" "$DEPLOY" >/dev/null
}

require_envoy_config() {  # the patch mounts it; missing → the pod never starts
  kubectl get cm -n "$NAMESPACE" envoy-config >/dev/null || {
    echo "error: ConfigMap envoy-config missing in $NAMESPACE (rendered by the platform chart)" >&2
    exit 1
  }
}

refuse_port_collision() {
  # An existing sidecar or the app on a sidecar port — see the header. Ports are
  # what a Deployment declares; an undeclared app port cannot be seen from here.
  local declared_ports p
  declared_ports="$(kubectl get deploy -n "$NAMESPACE" "$DEPLOY" \
    -o jsonpath='{range .spec.template.spec.containers[*].ports[*]}{.containerPort}{" "}{end}')"
  for p in 15124 15123 9090; do
    case " $declared_ports " in
      *" $p "*)
        echo "error: $DEPLOY already declares containerPort $p, which the lineage sidecar binds" >&2
        echo "  (an operator-injected sidecar, or the app itself on that port) — refusing to patch over it" >&2
        exit 1 ;;
    esac
  done
}

require_app_container() {
  # A strategic merge ADDS a stub container for an unknown name instead of
  # failing — so the name must exist. The generator cannot check this.
  [ -n "$APP_CONTAINER" ] || return 0
  local containers
  containers="$(kubectl get deploy -n "$NAMESPACE" "$DEPLOY" \
    -o jsonpath='{range .spec.template.spec.containers[*]}{.name}{" "}{end}')"
  case " $containers " in
    *" $APP_CONTAINER "*) ;;
    *)
      echo "error: deploy/$DEPLOY has no container named '$APP_CONTAINER' (it has: ${containers% })" >&2
      echo "  — refusing: the patch would ADD a stub container by that name instead of failing" >&2
      exit 1 ;;
  esac
}

note_capture_only() {
  # Whether the app propagates traceparent is a property of the app that
  # nothing here can see — say so once instead of guessing from its env.
  [ -z "$APP_CONTAINER" ] || return 0
  echo "NOTE: the sidecar records every hop; whether $DEPLOY's outbound hops attribute to" >&2
  echo "      their inbound depends on the app propagating traceparent itself (its own" >&2
  echo "      instrumentation, or the baked shim + APP_CONTAINER=<name>). Verify pairing" >&2
  echo "      under concurrency before relying on it (DESIGN.md, 'The envelope')." >&2
}

gen() {  # $1 = EMIT mode; the other knobs reach the generator through the environment
  EMIT="$1" NAME="$DEPLOY" SELF_ID="$SELF_ID" NAMESPACE="$NAMESPACE" \
    "${SCRIPT_DIR}/attach-lineage.sh"
}

apply() {
  # Both objects are generated before the first write, so a generator refusal
  # stops the script with nothing applied. ConfigMap first — the patch's
  # volume names it.
  local cm patch
  cm="$(gen cm)"
  patch="$(gen patch)"
  kubectl apply -f - <<<"$cm"
  kubectl patch deploy "$DEPLOY" -n "$NAMESPACE" --type strategic --patch "$patch"
  kubectl rollout status -n "$NAMESPACE" "deploy/$DEPLOY" --timeout=180s
  echo ">> lineage sidecar attached to deploy/$DEPLOY (self_id=$SELF_ID, ns=$NAMESPACE)"
}

preconditions() {  # read-only: each returns or exits — nothing is applied yet
  require_deployment
  require_envoy_config
  refuse_port_collision
  require_app_container
}

main() {
  read_inputs        # DEPLOY required; the rest defaulted or inherited
  preconditions      # four checks that can only stop the script
  note_capture_only  # no APP_CONTAINER → say what that means, once
  apply              # generate both, then the only cluster writes: cm → patch → rollout
}
main "$@"
