#!/usr/bin/env bash
# attach-lineage.sh — the one generator. Given a Deployment's NAME (and,
# optionally, which of its containers to switch propagation on and with which
# image), print ONE of the two objects that attach lineage to it:
#
#   EMIT=patch (default)  strategic-merge patch adding the sidecar pieces —
#                         proxy-init initContainer, envoy-proxy container, two
#                         config volumes — and, with APP_CONTAINER, the
#                         propagation switch on the app's own container.
#   EMIT=cm               the per-app plugin ConfigMap the sidecar mounts
#                         (parser chain + lineage-telemetry entry).
#
# Apply both and the app's traffic flows through the lineage plugin. The app
# itself is never described: no Deployment, no Service, no app config — lists
# merge by name, so nothing the owner wrote changes. Stdout only; this script
# never touches the cluster.
#
# Consumed by sidecar-patch.sh (live: checks, applies both, waits) or by your
# own kustomization (the cm under resources:, the patch under patches: with
# target kind Deployment / name NAME) — see README.md "How to attach".
#
# Propagation: capture alone cannot attribute an app's outbound calls to the
# inbound that caused them; the app must carry traceparent itself. For an
# uninstrumented Python app, bake the shim first (build-otel-shim.sh), then
# pass APP_CONTAINER (+ APP_IMAGE): the patch sets LINEAGE_PROPAGATE=1 on that
# container, which wakes the baked hook. Without APP_CONTAINER the patch is
# capture-only and the app container is not touched.
#
# Usage:
#   NAME=echo-upstream ./attach-lineage.sh                     # the patch
#   NAME=echo-upstream EMIT=cm ./attach-lineage.sh             # the ConfigMap
#   NAME=my-agent APP_CONTAINER=agent \
#     APP_IMAGE=docker.io/library/my-agent-otel:latest ./attach-lineage.sh
#
# Variables:
#   NAME            (required) the target Deployment; also names the ConfigMap
#                   (authbridge-lineage-config-NAME) and defaults SELF_ID
#   NAMESPACE       default team1
#   SELF_ID         lineage identity on every span (default NAME) — the one
#                   value that varies in the plugin config
#   APP_CONTAINER   the app container to set LINEAGE_PROPAGATE=1 on. MUST name
#                   an existing container: a strategic merge ADDS a stub for an
#                   unknown name. Checked by sidecar-patch.sh, not here.
#   APP_IMAGE       needs APP_CONTAINER: the -otel image to set on it
#   OTEL_ENDPOINT   OTLP/gRPC target for the plugin's spans (default: the
#                   platform collector). Any OTLP consumer works.
#   OUTBOUND_PORTS_EXCLUDE  ports proxy-init must not intercept — only an app's
#                   OWN telemetry export port (e.g. 4317). Never LLM/tool ports.
#   SIDECAR_IMAGE   default ghcr.io/rossoctl/cortex/authbridge-envoy:latest —
#                   UNTIL A RELEASE CARRIES lineage-telemetry it boots without
#                   the plugin; build from this repo and point this at your tag
#   PROXY_INIT_IMAGE  default ghcr.io/rossoctl/cortex/proxy-init:latest
#   NO_EMIT=1       omit the plugin entry: the sidecar proxies, emits nothing
#                   (parsers alone are legal). The A/B baseline.
#   EMIT            patch (default) | cm
#
# Structure: parse_inputs validates EVERY knob (all refusals live there);
# build_* each assemble one optional YAML fragment into a global; the three
# fragment functions are the single source of the sidecar YAML; emit()
# dispatches to the two emitters.
set -euo pipefail

# Every free-form value lands in a double-quoted YAML scalar, where only '"'
# and '\' are special — refusing those two is exactly sufficient. Whitespace is
# refused as well: it is never part of an endpoint or image ref.
yaml_safe() {  # $1 = what it is (for the error), $2 = the value
  local unsafe=$'"\\'
  case "$2" in
    *["$unsafe"]*|*[[:space:]]*)
      printf "error: %s '%s' contains whitespace or one of %s, which this script cannot quote safely\n" "$1" "$2" "$unsafe" >&2
      exit 2 ;;
  esac
}

parse_inputs() {
  # Knobs of the removed app-deployment mode (EMIT=manifest): a caller passing
  # one is running a stale recipe — refuse loudly rather than ignore.
  local stale
  for stale in IMAGE APP_PORT SVC_PORT ENV_VARS APP_COMMAND APP_RESOURCES \
               PVC_NAME PVC_MOUNT WORKLOAD_TYPE WORKLOAD_PROTOCOL LABEL_PREFIX \
               NO_PROPAGATE APP_ENTRYPOINT; do
    if [ -n "${!stale:-}" ]; then
      echo "error: $stale is not a knob — it was removed with the app-deployment (EMIT=manifest) mode." >&2
      echo "       This script only attaches lineage to an EXISTING Deployment; the app itself" >&2
      echo "       is deployed by its owner. See the header and README.md." >&2
      exit 2
    fi
  done

  EMIT="${EMIT:-patch}"
  NAME="${NAME:?set NAME}"
  NAMESPACE="${NAMESPACE:-team1}"
  # NAME (RFC 1123 name) and NAMESPACE (DNS label) are interpolated bare.
  if [ "${#NAME}" -gt 63 ] \
     || ! [[ "$NAME" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$ ]]; then
    echo "error: NAME='$NAME' must be a lowercase RFC 1123 name of at most 63 chars" >&2; exit 2
  fi
  if ! [[ "$NAMESPACE" =~ ^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$ ]]; then
    echo "error: NAMESPACE='$NAMESPACE' is not a DNS label (lowercase alphanumerics and '-', max 63)" >&2; exit 2
  fi
  case "$EMIT" in
    patch|cm) ;;
    *) echo "error: EMIT must be patch|cm (got '$EMIT')" >&2; exit 2 ;;
  esac
  SELF_ID="${SELF_ID:-$NAME}"
  APP_CONTAINER="${APP_CONTAINER:-}"
  APP_IMAGE="${APP_IMAGE:-}"
  OUTBOUND_PORTS_EXCLUDE="${OUTBOUND_PORTS_EXCLUDE:-}"
  OTEL_ENDPOINT="${OTEL_ENDPOINT:-otel-collector.rossoctl-system.svc.cluster.local:4317}"
  # Published images by default; point at local tags when building from source.
  SIDECAR_IMAGE="${SIDECAR_IMAGE:-ghcr.io/rossoctl/cortex/authbridge-envoy:latest}"
  PROXY_INIT_IMAGE="${PROXY_INIT_IMAGE:-ghcr.io/rossoctl/cortex/proxy-init:latest}"
  NO_EMIT="${NO_EMIT:-0}"

  local v
  for v in SELF_ID OTEL_ENDPOINT APP_IMAGE SIDECAR_IMAGE PROXY_INIT_IMAGE; do
    yaml_safe "$v" "${!v}"
  done
  # A container name is a DNS label; it is interpolated bare.
  if [ -n "$APP_CONTAINER" ] && ! [[ "$APP_CONTAINER" =~ ^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$ ]]; then
    echo "error: APP_CONTAINER='$APP_CONTAINER' is not a valid container name (DNS label)" >&2; exit 2
  fi
  if [ -n "$APP_IMAGE" ] && [ -z "$APP_CONTAINER" ]; then
    echo "error: APP_IMAGE needs APP_CONTAINER — the image lands on a container the patch must name" >&2; exit 2
  fi
  # Ports exactly as proxy-init hands them to iptables: no leading zero (iptables
  # reads `010` as octal and refuses `0080`), at most five digits (a longer
  # number overflows the `-gt` test below), then the range.
  [[ "$OUTBOUND_PORTS_EXCLUDE" =~ ^([1-9][0-9]{0,4}(,[1-9][0-9]{0,4})*)?$ ]] \
    || { echo "error: OUTBOUND_PORTS_EXCLUDE='$OUTBOUND_PORTS_EXCLUDE' is not a comma-separated list of ports (1-65535, no leading zeros)" >&2; exit 2; }
  local port
  for port in ${OUTBOUND_PORTS_EXCLUDE//,/ }; do
    if [ "$port" -gt 65535 ]; then
      echo "error: OUTBOUND_PORTS_EXCLUDE has '$port', which is not a port (1-65535)" >&2; exit 2
    fi
  done
}

build_proxy_env() {
  # proxy-init OUTBOUND_PORTS_EXCLUDE env (only if set)
  exclude_env=""
  if [ -n "$OUTBOUND_PORTS_EXCLUDE" ]; then
    exclude_env="
            - name: OUTBOUND_PORTS_EXCLUDE
              value: \"${OUTBOUND_PORTS_EXCLUDE}\""
  fi
}

build_plugin_entry() {
  # The plugin entry, one variable so NO_EMIT is a single point. Empty → the
  # parsers stay and the sidecar is a pure proxy that emits nothing.
  lineage_plugin=""
  if [ "$NO_EMIT" != "1" ]; then
    lineage_plugin='
          - name: lineage-telemetry
            config:
              otel_endpoint: "'"${OTEL_ENDPOINT}"'"
              capture_io: true
              self_id: "'"${SELF_ID}"'"'
  fi
}

build_app_patch() {
  # The propagation switch. `containers` and `env` both merge by name, so this
  # sets exactly LINEAGE_PROPAGATE (and the image, if given) on the owner's
  # container and nothing else. One env var is enough: it wakes the baked hook,
  # which sets the propagate-only posture itself as env defaults.
  app_patch=""
  if [ -n "$APP_CONTAINER" ]; then
    app_patch="
        - name: ${APP_CONTAINER}"
    if [ -n "$APP_IMAGE" ]; then
      app_patch="${app_patch}
          image: \"${APP_IMAGE}\""
    fi
    app_patch="${app_patch}
          env:
            - { name: LINEAGE_PROPAGATE, value: \"1\" }"
  fi
}

# ---- the sidecar fragments (the single source of every sidecar YAML byte) ----

sidecar_container() {  # the envoy-proxy container (8-space list-item indent)
  cat <<EOF
        # Envoy + authbridge-envoy (ext_proc + the lineage plugin). MUST run as
        # UID 1337: proxy-init exempts that uid from the outbound redirect.
        # Readiness = the inbound listener accepting; the admin port binds loopback.
        - name: envoy-proxy
          image: "${SIDECAR_IMAGE}"
          imagePullPolicy: IfNotPresent
          args: ["--config", "/etc/authbridge/config.yaml"]
          securityContext:
            runAsNonRoot: true
            runAsUser: 1337
            runAsGroup: 1337
            allowPrivilegeEscalation: false
            capabilities:
              drop: ["ALL"]
          ports:
            - { containerPort: 15123, name: envoy-out }
            - { containerPort: 15124, name: envoy-in }
            - { containerPort: 9090,  name: ext-proc }
          readinessProbe:
            tcpSocket: { port: 15124 }
            initialDelaySeconds: 2
            periodSeconds: 5
          resources:
            requests: { cpu: 50m, memory: 64Mi }
            limits: { cpu: 500m, memory: 512Mi }
          volumeMounts:
            - { name: envoy-config,       mountPath: /etc/envoy,      readOnly: true }
            - { name: authbridge-runtime, mountPath: /etc/authbridge, readOnly: true }
EOF
}

proxy_init_container() {  # the iptables init container
  cat <<EOF
        # Programs the pod's iptables once and exits. Root + exactly
        # NET_ADMIN/NET_RAW, nothing else.
        - name: proxy-init
          image: "${PROXY_INIT_IMAGE}"
          imagePullPolicy: IfNotPresent
          securityContext:
            runAsNonRoot: false
            runAsUser: 0
            allowPrivilegeEscalation: false
            capabilities:
              drop: ["ALL"]
              add: ["NET_ADMIN", "NET_RAW"]
          resources:
            requests: { cpu: 10m, memory: 16Mi }
            limits: { cpu: 100m, memory: 64Mi }
          env:
            - name: POD_IP
              valueFrom:
                fieldRef:
                  fieldPath: status.podIP${exclude_env}
EOF
}

sidecar_volumes() {  # envoy-config + the per-app runtime ConfigMap
  cat <<EOF
        - name: envoy-config
          configMap:
            name: envoy-config
        - name: authbridge-runtime
          configMap:
            name: authbridge-lineage-config-${NAME}
            items:
              - { key: config.yaml, path: config.yaml }
EOF
}

# ---- the two emittable objects ----

emit_configmap() {
  cat <<EOF
apiVersion: v1
kind: ConfigMap
metadata:
  name: authbridge-lineage-config-${NAME}
  namespace: ${NAMESPACE}
data:
  config.yaml: |
    mode: envoy-sidecar
    # The same parser chain in both directions: an app's entry protocol is
    # not knowable from the attach side, and a direction-specific chain would
    # silently mislabel what it was not given as plain http. Uniform is safe —
    # parsers are content-gated, and the plugin reads payloads only through
    # the protocol fact it stamped (precedence a2a > mcp > inference).
    pipeline:
      inbound:
        plugins:
          - name: a2a-parser
          - name: mcp-parser
          - name: inference-parser${lineage_plugin}
      outbound:
        plugins:
          - name: a2a-parser
          - name: mcp-parser
          - name: inference-parser${lineage_plugin}
EOF
}

emit_patch() {  # strategic merge: lists merge by name — the owner's spec is untouched
  # apiVersion/kind/metadata make it a complete resource, which kustomize
  # requires of a patch file; kubectl patch merges them harmlessly.
  cat <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ${NAME}
  namespace: ${NAMESPACE}
spec:
  template:
    spec:
      initContainers:
$(proxy_init_container)
      containers:
$(sidecar_container)${app_patch}
      volumes:
$(sidecar_volumes)
EOF
}

emit() {
  case "$EMIT" in
    cm)    emit_configmap ;;
    patch) emit_patch ;;
  esac
}

main() {
  parse_inputs        # every knob: read, default, validate — all refusals live here
  build_proxy_env     # optional OUTBOUND_PORTS_EXCLUDE env for proxy-init
  build_plugin_entry  # the lineage-telemetry pipeline entry (empty under NO_EMIT=1)
  build_app_patch     # optional propagation switch on the app's own container
  emit                # dispatch: patch | cm
}
main "$@"
