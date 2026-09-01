#!/usr/bin/env bash
# sidecar-patch.sh — attach the lineage sidecar to an EXISTING Deployment.
#
# The live applier: the Deployment already exists — deployed by its owner,
# whoever that is — and must keep its owner's spec. This script only ADDS the
# lineage pieces via a strategic-merge patch (lists merge by name: the app
# container is untouched unless APP_CONTAINER opts it in; interception is
# transparent iptables — no HTTP_PROXY, no code change).
#
# Every YAML byte comes from attach-lineage.sh, the ONE generator (EMIT=cm for
# the per-app plugin ConfigMap, EMIT=patch for the sidecar patch) — this script
# only applies them and waits. To keep the attachment in version control
# instead of patching live, consume the same two outputs in a kustomization
# (README.md "Bring your own manifests"). CAVEAT the owner keeps owning the
# object: a platform rewrite of the Deployment silently drops the patch
# (observed live when an operator reconciled a patched Deployment) — re-run
# this script after any platform-side change.
#
# The target must NOT already carry a platform-injected AuthBridge sidecar
# (a Deployment enrolled through an AgentRuntime CR / `<prefix>/inject:
# enabled` has one): the operator's sidecar is also a container named
# `envoy-proxy`, so a strategic merge would silently MERGE into it — image,
# args, securityContext, volumes re-pointed at this ConfigMap — not sit beside
# it. Nor may the app itself listen on 9090, 15123 or 15124 — the sidecar
# binds those in the shared pod network namespace. Both are checked below.
#
# NOTE: natively-instrumented apps need no shim — in-process context already
# propagates. Apps that are NOT instrumented still need the shim for correct
# pairing under concurrency: bake it onto the app image (build-otel-shim.sh),
# then run this script with APP_CONTAINER (and APP_IMAGE pointing at the baked
# -otel image) so the patch flips the activation env on the app's own
# container. See README.md "The propagation half".
#
# Usage (env-driven, like attach-lineage.sh):
#   DEPLOY=echo-upstream ./sidecar-patch.sh
#   DEPLOY=my-agent APP_CONTAINER=agent \
#     APP_IMAGE=docker.io/library/my-agent-otel:latest ./sidecar-patch.sh
#
# Env:
#   DEPLOY                  target Deployment name (required)
#   NAMESPACE               default team1
#   SELF_ID                 lineage self_id (default: $DEPLOY)
#   OTEL_ENDPOINT           OTLP/gRPC endpoint the lineage plugin exports to
#                           (default otel-collector.rossoctl-system.svc.cluster.local:4317).
#   APP_CONTAINER           the app container's name in the target Deployment —
#                           when set, the patch also adds LINEAGE_PROPAGATE=1
#                           to that container's env (propagation on).
#   APP_IMAGE               with APP_CONTAINER: the -otel image ref to set on
#                           the app container (build-otel-shim.sh output).
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
#
# Structure: main() at the bottom is the pipeline — read_inputs, then the four
# preconditions (each a require_*/refuse_* function that exits before anything
# is applied), the capture-only NOTE, then apply (ConfigMap first, patch, wait).
# gen() is the one bridge to the generator.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

read_inputs() {
  DEPLOY="${DEPLOY:?usage: DEPLOY=<deployment> [NAMESPACE=team1] [SELF_ID=<id>] [APP_CONTAINER=<name> [APP_IMAGE=<ref>]] [OUTBOUND_PORTS_EXCLUDE=ports] sidecar-patch.sh}"
  NAMESPACE="${NAMESPACE:-team1}"
  SELF_ID="${SELF_ID:-$DEPLOY}"
  APP_CONTAINER="${APP_CONTAINER:-}"
  # Everything else (APP_IMAGE, *_IMAGE, OTEL_ENDPOINT, OUTBOUND_PORTS_EXCLUDE)
  # is inherited by attach-lineage.sh from the environment and validated there.
}

require_deployment() {
  kubectl get deploy -n "$NAMESPACE" "$DEPLOY" >/dev/null
}

require_envoy_config() {
  # The patch mounts it; without it the new pod sits in ContainerCreating.
  kubectl get cm -n "$NAMESPACE" envoy-config >/dev/null || {
    echo "error: ConfigMap envoy-config missing in $NAMESPACE (rendered by the platform chart)" >&2
    exit 1
  }
}

refuse_port_collision() {
  # Refuse a target whose pod already binds a port the sidecar needs: either an
  # injected AuthBridge sidecar is present (15123/15124/9090 all taken — the
  # patch would merge INTO that `envoy-proxy` container and re-point it at this
  # ConfigMap, silently taking it away from the operator), or the app itself
  # listens on one of them. Detected by declared container ports rather than a
  # container name, which the operator owns. An app port that is not declared
  # cannot be seen from here.
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
  # APP_CONTAINER must name a container that actually exists: a strategic
  # merge ADDS a stub container for an unknown name rather than failing, so a
  # typo would silently grow the pod a broken extra container.
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
  # Without APP_CONTAINER this attaches capture only. Whether the app carries
  # trace context from its inbound request to its outbound calls is a property
  # of the app (its own instrumentation, or the baked shim activated via
  # APP_CONTAINER) that nothing here can see — say so once, at attach time,
  # instead of guessing from the Deployment's env.
  [ -z "$APP_CONTAINER" ] || return 0
  echo "NOTE: the sidecar records every hop; whether $DEPLOY's outbound hops attribute to" >&2
  echo "      their inbound depends on the app propagating traceparent itself (its own" >&2
  echo "      instrumentation, or the baked shim + APP_CONTAINER=<name>). Verify pairing" >&2
  echo "      under concurrency before relying on it (README.md, 'The envelope')." >&2
}

gen() {  # $1 = EMIT mode; forwards the shared knobs to the one generator
  EMIT="$1" NAME="$DEPLOY" SELF_ID="$SELF_ID" NAMESPACE="$NAMESPACE" \
    "${SCRIPT_DIR}/attach-lineage.sh"
}

apply() {
  # ConfigMap first: the patch's volume names it, and the new pod cannot start
  # without it. The patch goes through its own assignment so a generator
  # failure stops the script (a $(...) inside an argument would not).
  local patch
  gen cm | kubectl apply -f -
  patch="$(gen patch)"
  kubectl patch deploy "$DEPLOY" -n "$NAMESPACE" --type strategic --patch "$patch"
  kubectl rollout status -n "$NAMESPACE" "deploy/$DEPLOY" --timeout=180s
  echo ">> lineage sidecar attached to deploy/$DEPLOY (self_id=$SELF_ID, ns=$NAMESPACE)"
}

main() {
  read_inputs             # DEPLOY required; the rest defaulted or inherited
  require_deployment      # the target exists
  require_envoy_config    # the platform's Envoy config is in the namespace
  refuse_port_collision   # no sidecar there already, app not on a sidecar port
  require_app_container   # APP_CONTAINER, if given, is a real container
  note_capture_only       # no APP_CONTAINER → say what that means, once
  apply                   # cm → patch → rollout
}
main "$@"
