#!/usr/bin/env bash
# Lineage sidecar attachment. Emits (to stdout) a complete manifest — lineage
# ConfigMap + Service + Deployment — that runs ANY app image with:
#   (a) the AuthBridge lineage sidecar (proxy-init initContainer + envoy-proxy sidecar,
#       AuthBridge envoy-sidecar mode, capture_io:true, no auth/SPIRE), and
#   (b) the propagate-only OTel shim switched ON: one env var
#       (LINEAGE_PROPAGATE=1) activates the hook baked into the -otel image
#       (build-otel-shim.sh). The app's own ENTRYPOINT/CMD is NEVER touched —
#       no command: is emitted at all; the shim exports nothing and only lets
#       the W3C traceparent flow through the app.
#
# Per app, only a handful of variables change (image + self_id + app env).
#
# Usage (pipe to kubectl apply):
#   NAME=my-agent \
#   IMAGE=docker.io/library/my-agent-otel:latest \
#   APP_PORT=8000 SVC_PORT=8080 \
#   ENV_VARS='LLM_API_BASE=<openai-compatible-base-url> LLM_MODEL=<model> LLM_API_KEY=<key>' \
#   demos/lineage/attach-lineage.sh | kubectl apply -f -
#
# Variables:
#   NAME               (required) k8s resource name + app.kubernetes.io/name label.
#   IMAGE              (required for EMIT=manifest) the -otel wrapper image
#                      (build-otel-shim.sh output).
#   SELF_ID            lineage self_id (default: NAME). Only this varies in the config.
#   APP_PORT           app container port (default 8000).
#   SVC_PORT           service port (default 8080).
#   APP_COMMAND        optional container command tokens, run AS-IS (no wrapper
#                      of any kind). A DEPLOY choice, not an instrumentation
#                      input: set it only when an image hosts several programs
#                      and this workload runs a non-default one. The shim
#                      attaches via env either way. Default: empty — the
#                      image's own ENTRYPOINT/CMD runs untouched.
#   ENV_VARS           space-separated KEY=VALUE app env (LLM_* etc). Each
#                      value is emitted as a double-quoted YAML scalar, so a
#                      value may contain ':' '#' '{' etc. but NOT whitespace,
#                      '"' or '\' — the script refuses those rather than
#                      emit YAML that parses to something else. May include
#                      OTEL_SERVICE_NAME to override the default (SELF_ID).
#   APP_RESOURCES      the app container's `resources:` block as a one-line
#                      YAML flow mapping (default: requests 100m/128Mi,
#                      limits 1 CPU/1Gi; `{}` removes them — your choice,
#                      spliced verbatim). Sidecar containers carry fixed
#                      requests/limits (see sidecar_container/proxy_init_container).
#   OUTBOUND_PORTS_EXCLUDE  iptables outbound excludes (default ''). Set to an
#                      app's OWN OTLP export port (e.g. 4317/4318) for an app that
#                      already exports spans, so that export keeps flowing
#                      untouched. Do NOT exclude LLM/tool ports — we want those seen.
#   PVC_NAME/PVC_MOUNT optional: mount an existing-or-created RWO PVC into the
#                      app container at PVC_MOUNT (both must be set).
#   NAMESPACE          (default team1, the platform's demo namespace).
#   OTEL_ENDPOINT      OTLP/gRPC endpoint the lineage plugin exports to (default
#                      otel-collector.rossoctl-system.svc.cluster.local:4317).
#                      Any OTLP consumer works — Phoenix and Jaeger included;
#                      nothing downstream of this endpoint is assumed.
#   WORKLOAD_TYPE      agent | tool. **Empty by default, and leaving it empty
#                      is the right choice on the rossoctl platform**, which
#                      forbids setting <prefix>/type by hand: a
#                      ValidatingAdmissionPolicy reserves that label for the
#                      operator, so a manifest carrying it is REJECTED at
#                      admission. Omitting it costs only platform-UI
#                      registration (use an AgentRuntime CR for that); it costs
#                      nothing for lineage. Set it only on a platform you know
#                      does not guard the label.
#   WORKLOAD_PROTOCOL  protocol label value (default a2a). Set mcp for MCP
#                      servers — the label is a factual claim a platform UI
#                      reads — or empty to omit it. Presentational only.
#   LABEL_PREFIX       platform label domain (default rossoctl.io). Sets
#                      protocol.<prefix>/<WORKLOAD_PROTOCOL>, the
#                      <prefix>/inject: disabled opt-out, and <prefix>/type
#                      when WORKLOAD_TYPE is set. Retarget it for a
#                      differently-branded platform.
#   SIDECAR_IMAGE      envoy+authbridge sidecar image (default
#                      ghcr.io/rossoctl/cortex/authbridge-envoy:latest).
#                      UNTIL A RELEASE CARRIES THE lineage-telemetry PLUGIN
#                      the published default boots without it (the sidecar
#                      logs `unknown plugin "lineage-telemetry"`): build the
#                      image from this repo and point this at your tag.
#   PROXY_INIT_IMAGE   iptables init image (default
#                      ghcr.io/rossoctl/cortex/proxy-init:latest); build it
#                      alongside SIDECAR_IMAGE when building from source.
#   NO_PROPAGATE       if set to 1, omit LINEAGE_PROPAGATE=1 from the app env:
#                      the -otel image's hook stays dormant and the container
#                      runs EXACTLY like the base image — trace context stops
#                      flowing THROUGH the app, and nothing more (the sidecar
#                      still emits). For a baseline/uninstrumented run.
#   NO_EMIT            if set to 1, omit the lineage-telemetry plugin from the
#                      generated ConfigMap: the sidecar emits zero spans — and
#                      nothing more (it still proxies; parsers stay — legal
#                      alone; the plugin declares RequiresAny{parsers}, not the
#                      reverse). NO_PROPAGATE=1 NO_EMIT=1 together = lineage
#                      fully off for this app.
#   EMIT               what to emit (this script is the ONE source of every
#                      lineage YAML byte):
#                        manifest  (default) ConfigMap + Service + Deployment
#                        cm        the per-app lineage ConfigMap alone
#                        patch     a strategic-merge patch adding the sidecar
#                                  pieces to an EXISTING Deployment (used by
#                                  sidecar-patch.sh; app container untouched)
#
# Structure: main() at the bottom is the pipeline — parse_inputs reads,
# defaults and validates EVERY knob (all refusals live there; nothing after
# it can reject), the build_* functions each assemble one YAML fragment into
# a global, and emit() dispatches to the emitters. The three shared fragment
# functions (sidecar_container / proxy_init_container / sidecar_volumes) are
# the single source of the sidecar YAML for both the manifest and the patch.
set -euo pipefail

# ---- input guard for caller-supplied values ----
# Every free-form caller value lands in the generated YAML — some inside a
# double-quoted scalar (SELF_ID, OTEL_ENDPOINT, ENV_VARS values, APP_COMMAND
# tokens, OUTBOUND_PORTS_EXCLUDE), some bare (IMAGE, LABEL_PREFIX, PVC_*,
# WORKLOAD_*). In a double-quoted scalar only '"' and '\' are special; bare,
# whitespace or ':' would be. A value carrying '"', '\' or whitespace
# (word-splitting has already mangled the latter by the time we see a token)
# would be emitted as YAML that parses to something other than what the
# caller meant — refuse instead of guessing. Ports are integers, the exclude
# list is integers separated by commas (what proxy-init accepts).
yaml_safe() {  # $1 = what it is (for the error), $2 = the value
  local unsafe=$'"\\'
  case "$2" in
    *["$unsafe"]*|*[[:space:]]*)
      printf "error: %s '%s' contains whitespace or one of %s, which this script cannot quote safely\n" "$1" "$2" "$unsafe" >&2
      exit 2 ;;
  esac
}

parse_inputs() {
  EMIT="${EMIT:-manifest}"
  NAME="${NAME:?set NAME}"
  NAMESPACE="${NAMESPACE:-team1}"
  # NAME becomes object names AND the app.kubernetes.io/name label value, so it
  # must satisfy both: lowercase RFC 1123 (dots allowed) within a label's 63
  # chars. NAMESPACE is a DNS label. Both are then safe everywhere they are
  # interpolated bare.
  if ! [[ "$NAME" =~ ^[a-z0-9]([-a-z0-9.]{0,61}[a-z0-9])?$ ]]; then
    echo "error: NAME='$NAME' must be a lowercase RFC 1123 name of at most 63 chars (it is also used as a label value)" >&2; exit 2
  fi
  if ! [[ "$NAMESPACE" =~ ^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$ ]]; then
    echo "error: NAMESPACE='$NAMESPACE' is not a DNS label (lowercase alphanumerics and '-', max 63)" >&2; exit 2
  fi
  case "$EMIT" in
    manifest) IMAGE="${IMAGE:?set IMAGE}" ;;
    cm|patch) IMAGE="${IMAGE:-}" ;;
    *) echo "error: EMIT must be manifest|cm|patch (got '$EMIT')" >&2; exit 2 ;;
  esac
  SELF_ID="${SELF_ID:-$NAME}"
  APP_PORT="${APP_PORT:-8000}"
  SVC_PORT="${SVC_PORT:-8080}"
  # The shim activates via env; the app command is never known here. A caller
  # still passing a command-wrap knob is running a stale recipe — refuse so the
  # difference is loud, not silent.
  if [ -n "${APP_ENTRYPOINT:-}" ]; then
    echo "error: APP_ENTRYPOINT is not a knob — the -otel image activates via" >&2
    echo "       LINEAGE_PROPAGATE=1 (this script sets it) and the app's own" >&2
    echo "       ENTRYPOINT/CMD runs untouched. Remove APP_ENTRYPOINT." >&2
    exit 2
  fi
  ENV_VARS="${ENV_VARS:-}"
  APP_COMMAND="${APP_COMMAND:-}"
  APP_RESOURCES_DEFAULT='{ requests: { cpu: 100m, memory: 128Mi }, limits: { cpu: "1", memory: 1Gi } }'
  APP_RESOURCES="${APP_RESOURCES:-$APP_RESOURCES_DEFAULT}"
  OUTBOUND_PORTS_EXCLUDE="${OUTBOUND_PORTS_EXCLUDE:-}"
  PVC_NAME="${PVC_NAME:-}"
  PVC_MOUNT="${PVC_MOUNT:-}"
  OTEL_ENDPOINT="${OTEL_ENDPOINT:-otel-collector.rossoctl-system.svc.cluster.local:4317}"
  LABEL_PREFIX="${LABEL_PREFIX:-rossoctl.io}"
  WORKLOAD_TYPE="${WORKLOAD_TYPE:-}"
  WORKLOAD_PROTOCOL="${WORKLOAD_PROTOCOL:-a2a}"
  # Published images by default so the demo runs against a stock platform; point
  # these at locally-built tags when you build the sidecar from source.
  SIDECAR_IMAGE="${SIDECAR_IMAGE:-ghcr.io/rossoctl/cortex/authbridge-envoy:latest}"
  PROXY_INIT_IMAGE="${PROXY_INIT_IMAGE:-ghcr.io/rossoctl/cortex/proxy-init:latest}"
  # Each toggle kills exactly one layer and nothing more.
  NO_PROPAGATE="${NO_PROPAGATE:-0}"
  NO_EMIT="${NO_EMIT:-0}"

  local v
  for v in SELF_ID OTEL_ENDPOINT IMAGE LABEL_PREFIX WORKLOAD_TYPE WORKLOAD_PROTOCOL PVC_NAME PVC_MOUNT; do
    yaml_safe "$v" "${!v}"
  done
  for v in APP_PORT SVC_PORT; do
    [[ "${!v}" =~ ^[0-9]+$ ]] || { echo "error: $v='${!v}' is not a port number" >&2; exit 2; }
  done
  [[ "$OUTBOUND_PORTS_EXCLUDE" =~ ^([0-9]+(,[0-9]+)*)?$ ]] \
    || { echo "error: OUTBOUND_PORTS_EXCLUDE='$OUTBOUND_PORTS_EXCLUDE' is not a comma-separated port list" >&2; exit 2; }
  local tok kv
  for tok in $APP_COMMAND; do
    yaml_safe "APP_COMMAND token" "$tok"
  done
  for kv in $ENV_VARS; do
    if ! [[ "$kv" =~ ^[A-Za-z_][A-Za-z0-9_]*= ]]; then
      echo "error: ENV_VARS entry '$kv' is not KEY=VALUE (a value with whitespace?)" >&2; exit 2
    fi
    yaml_safe "ENV_VARS value for ${kv%%=*}" "${kv#*=}"
  done
}

build_labels() {
  # Emitted only when the caller asks for it — see the header. Selectors key on
  # app.kubernetes.io/name alone, which is already unique per workload, so the
  # label is presentational and its absence changes nothing functional.
  type_label=""
  type_label_8=""
  if [ -n "$WORKLOAD_TYPE" ]; then
    type_label="
    ${LABEL_PREFIX}/type: ${WORKLOAD_TYPE}"
    type_label_8="
        ${LABEL_PREFIX}/type: ${WORKLOAD_TYPE}"
  fi
  # Protocol label: a factual claim a platform UI reads, so it must be true —
  # an MCP tool must not advertise protocol.<prefix>/a2a. Defaults to a2a for
  # the common case; set WORKLOAD_PROTOCOL=mcp for MCP servers, or empty to
  # omit the label entirely. Presentational only, like the type label.
  proto_label=""
  proto_label_8=""
  if [ -n "$WORKLOAD_PROTOCOL" ]; then
    proto_label="
    protocol.${LABEL_PREFIX}/${WORKLOAD_PROTOCOL}: \"\""
    proto_label_8="
        protocol.${LABEL_PREFIX}/${WORKLOAD_PROTOCOL}: \"\""
  fi
}

build_app_env() {
  # The whole propagation switch is ONE env var: LINEAGE_PROPAGATE=1 wakes the
  # hook baked into the -otel image, which pins the propagate-only posture
  # itself (all exporters none, tracecontext+baggage propagators — as env
  # DEFAULTS, so a deliberate override here still wins). Under NO_PROPAGATE=1
  # the var is simply absent and the container runs exactly like the base
  # image; nothing OTel-shaped is emitted into an unshimmed app's environment.
  env_block=$(cat <<EOF
            - { name: PORT, value: "${APP_PORT}" }
            - { name: HOST, value: "0.0.0.0" }
            # For the IMAGE's own command, not for the shim (which runs no
            # launcher): the example agent images start through 'uv run
            # --no-sync', and under a uid with no home directory that
            # crash-loops on 'failed to create directory /.cache/uv'
            # (observed). HOME=/tmp + UV_NO_CACHE sidesteps every cache-dir
            # variant; harmless for images that do not launch via uv.
            - { name: HOME, value: "/tmp" }
            - { name: UV_NO_CACHE, value: "1" }
EOF
  )
  if [ "$NO_PROPAGATE" != "1" ]; then
    env_block="            - { name: LINEAGE_PROPAGATE,   value: \"1\" }
${env_block}"
  fi
  # OTEL_SERVICE_NAME defaults to SELF_ID — the shim exports nothing, but an
  # app that runs its OWN exporter then reports under its lineage identity.
  # Skipped when the caller supplies one via ENV_VARS so the caller's value
  # wins (no duplicate entry).
  case " $ENV_VARS" in
    *" OTEL_SERVICE_NAME="*) ;;
    *) env_block="${env_block}
            - { name: OTEL_SERVICE_NAME,     value: \"${SELF_ID}\" }" ;;
  esac
  local kv
  for kv in $ENV_VARS; do
    env_block="${env_block}
            - { name: ${kv%%=*}, value: \"${kv#*=}\" }"
  done
}

build_command() {
  # Optional command override (see APP_COMMAND in the header) — emitted
  # verbatim as command: — replaces the image ENTRYPOINT+CMD with the
  # caller's tokens and nothing else; propagation still rides the env gate.
  command_line=""
  if [ -n "$APP_COMMAND" ]; then
    local cmd_tokens="" tok
    for tok in $APP_COMMAND; do
      cmd_tokens="${cmd_tokens}\"${tok}\","
    done
    command_line="
          command: [${cmd_tokens%,}]"
  fi
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

build_pvc() {
  # Optional shared PVC mounted into the app container (only if set).
  # `kubectl apply` of the same PVC from several manifests is idempotent — a
  # claim may be shared on purpose (writer + reader pods on single-node kind).
  pvc_manifest=""; pvc_mount_yaml=""; pvc_volume_yaml=""
  if [ -n "$PVC_NAME" ] && [ -n "$PVC_MOUNT" ]; then
    pvc_manifest="---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: ${PVC_NAME}
  namespace: ${NAMESPACE}
spec:
  accessModes: [\"ReadWriteOnce\"]
  resources: { requests: { storage: 100Mi } }"
    pvc_mount_yaml="
          volumeMounts:
            - { name: share, mountPath: ${PVC_MOUNT} }"
    pvc_volume_yaml="
        - name: share
          persistentVolumeClaim:
            claimName: ${PVC_NAME}"
  fi
}

build_plugin_entry() {
  # lineage-telemetry pipeline entry — one variable so ON/OFF is a single point.
  # Empty under NO_EMIT=1: the parsers remain (content-gated, harmless alone)
  # and the sidecar becomes a pure proxy that emits nothing.
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

# ---- shared fragments (identical bytes in EMIT=manifest and EMIT=patch) ----

sidecar_container() {  # the envoy-proxy container (8-space list-item indent)
  cat <<EOF
        # lineage sidecar: Envoy + authbridge-envoy(ext_proc + lineage plugin).
        # MUST run as UID 1337 (excluded from the iptables outbound redirect).
        # Only the three ports the pod needs are declared; the Envoy admin
        # port binds loopback and nothing here reads it. Readiness is the
        # inbound listener accepting: without it a Service endpoint goes
        # Ready before the sidecar can take the redirected connection.
        - name: envoy-proxy
          image: ${SIDECAR_IMAGE}
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
            - { name: envoy-config,       mountPath: /etc/envoy }
            - { name: authbridge-runtime, mountPath: /etc/authbridge }
EOF
}

proxy_init_container() {  # the iptables init container
  cat <<EOF
        # iptables needs root + NET_ADMIN/NET_RAW (proxy-init's README) and
        # nothing else: everything is dropped first, then exactly those two
        # are added back.
        - name: proxy-init
          image: ${PROXY_INIT_IMAGE}
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

# ---- the four emittable objects ----

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
    # SAME parser chain in both directions. An app's entry protocol is not
    # knowable from the attach side: an A2A agent receives a2a and calls mcp,
    # an MCP tool receives mcp, and either may call an inference endpoint. A
    # direction-specific chain silently mislabels whatever it wasn't given —
    # an MCP-entry tool with an a2a-only inbound chain records its tools/call
    # as anonymous http, indistinguishable downstream from infrastructure
    # traffic.
    #
    # Uniform is safe because every parser is content-gated and the lineage
    # plugin reads payloads ONLY through the protocol fact it stamped
    # (protocolOf precedence: a2a > mcp > inference). The parsers are NOT
    # mutually exclusive — mcp-parser attaches to any JSON-RPC body, so on
    # every a2a exchange both extensions are populated; precedence picks the
    # label and the protocol-keyed payload read keeps the wrong parser's
    # output from ever landing on a span. Non-matching traffic falls through
    # untouched as plain http.
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

emit_service() {
  cat <<EOF
apiVersion: v1
kind: Service
metadata:
  name: ${NAME}
  namespace: ${NAMESPACE}
  labels:
    app.kubernetes.io/name: ${NAME}${type_label}${proto_label}
spec:
  ports:
    - name: http
      port: ${SVC_PORT}
      protocol: TCP
      targetPort: ${APP_PORT}
  selector:
    app.kubernetes.io/name: ${NAME}
  type: ClusterIP
EOF
}

emit_deployment() {
  cat <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ${NAME}
  namespace: ${NAMESPACE}
  labels:
    app.kubernetes.io/name: ${NAME}${type_label}
spec:
  replicas: 1
  selector:
    matchLabels:
      app.kubernetes.io/name: ${NAME}
  template:
    metadata:
      labels:
        app.kubernetes.io/name: ${NAME}${type_label_8}${proto_label_8}
        # opt out of operator-injected sidecars: this manifest injects its own
        # (auth-free, capture-only) sidecar below, and two would collide.
        ${LABEL_PREFIX}/inject: disabled
    spec:
      securityContext:
        seccompProfile: { type: RuntimeDefault }
      containers:
        # Normally no command: — the image's own ENTRYPOINT/CMD runs as
        # built; the propagation shim (when present and gated on via env)
        # attaches at interpreter startup, not by command rewrite. APP_COMMAND
        # (multi-program images only) is the one exception and carries no
        # wrapper either.
        - name: agent
          image: ${IMAGE}
          imagePullPolicy: IfNotPresent${command_line}
          # The app keeps the uid its image declares (root in some images, so
          # runAsNonRoot is not forced here); it gets no capabilities and no
          # privilege escalation regardless.
          securityContext:
            allowPrivilegeEscalation: false
            capabilities:
              drop: ["ALL"]
          ports:
            - { containerPort: ${APP_PORT}, name: http }
          env:
${env_block}
          resources: ${APP_RESOURCES}
          readinessProbe:
            tcpSocket: { port: ${APP_PORT} }
            initialDelaySeconds: 5
            periodSeconds: 5${pvc_mount_yaml}
$(sidecar_container)
      initContainers:
$(proxy_init_container)
      volumes:
$(sidecar_volumes)${pvc_volume_yaml}
EOF
}

emit_patch() {  # strategic merge: lists merge by name — the app container is untouched
  cat <<EOF
spec:
  template:
    spec:
      initContainers:
$(proxy_init_container)
      containers:
$(sidecar_container)
      volumes:
$(sidecar_volumes)
EOF
}

emit_manifest() {
  cat <<EOF
# GENERATED by demos/lineage/attach-lineage.sh — do not hand-edit; re-run.
# app=${NAME} self_id=${SELF_ID} image=${IMAGE} propagate=$([ "$NO_PROPAGATE" = 1 ] && echo off || echo on) emit=$([ "$NO_EMIT" = 1 ] && echo off || echo on)${pvc_manifest}
---
$(emit_configmap)
---
$(emit_service)
---
$(emit_deployment)
EOF
}

emit() {
  case "$EMIT" in
    cm)       emit_configmap ;;
    patch)    emit_patch ;;
    manifest) emit_manifest ;;
  esac
}

main() {
  parse_inputs        # every knob: read, default, validate — all refusals live here
  build_labels        # optional type/protocol label fragments (two indent depths)
  build_app_env       # app env block: the gate var + uv fix + OTEL_SERVICE_NAME + ENV_VARS
  build_command       # optional verbatim command: line from APP_COMMAND
  build_proxy_env     # optional OUTBOUND_PORTS_EXCLUDE env for proxy-init
  build_pvc           # optional PVC manifest + mount + volume fragments
  build_plugin_entry  # the lineage-telemetry pipeline entry (empty under NO_EMIT=1)
  emit                # dispatch: manifest | cm | patch
}
main "$@"
