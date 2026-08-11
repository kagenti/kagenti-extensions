#!/usr/bin/env bash
# GENERALIZED lineage-sidecar attachment. Emits (to stdout) a complete manifest —
# lineage ConfigMap + Service + Deployment — that runs ANY app image with:
#   (a) the AuthBridge lineage sidecar (proxy-init initContainer + envoy-proxy sidecar,
#       AuthBridge envoy-sidecar mode, capture_io:true, no auth/SPIRE), and
#   (b) the propagate-only OTEL shim launcher wrapping the app command
#       (--traces_exporter none: export nothing, only propagate traceparent).
#
# Per app, only a handful of variables change (image + self_id + the app's own
# entrypoint + LLM env).
#
# Usage (pipe to kubectl apply):
#   NAME=a2a-currency-converter \
#   IMAGE=docker.io/library/a2a_currency_converter-otel:latest \
#   APP_PORT=8000 SVC_PORT=8080 \
#   APP_ENTRYPOINT='app --host 0.0.0.0 --port 8000' \
#   ENV_VARS='LLM_API_BASE=http://host.containers.internal:11434/v1 LLM_MODEL=qwen2.5:7b LLM_API_KEY=ollama' \
#   demos/lineage-adapter/attach-lineage.sh | kubectl apply -f -
#
# Variables:
#   NAME               (required) k8s resource name + app.kubernetes.io/name label.
#   IMAGE              (required for EMIT=manifest) the -otel wrapper image
#                      (build-otel-shim.sh output).
#   SELF_ID            lineage self_id (default: NAME). Only this varies in the config.
#   APP_PORT           app container port (default 8000).
#   SVC_PORT           service port (default 8080).
#   APP_ENTRYPOINT     the app's OWN command tokens (default 'server').
#                      Check the app image's Dockerfile CMD,
#                      dropping any leading `uv run --no-sync`.
#   ENV_VARS           space-separated KEY=VALUE app env (LLM_* etc). Values must
#                      not contain spaces (fine for our URLs/models). May include
#                      OTEL_SERVICE_NAME to override the default (SELF_ID).
#   OUTBOUND_PORTS_EXCLUDE  iptables outbound excludes (default ''). Set to an
#                      app's OWN OTLP export port (e.g. 8335/4318) for an app that
#                      already exports spans, so that export keeps flowing
#                      untouched. Do NOT exclude LLM/tool ports — we want those seen.
#   NAMESPACE          (default team1).
#   OTEL_ENDPOINT      collector the lineage plugin exports to (default
#                      otel-collector.kagenti-system.svc.cluster.local:4317;
#                      rossoctl-branded platforms: otel-collector.rossoctl-system...).
#   KAGENTI_TYPE       kagenti.io/type label: agent | tool (default agent).
#   NO_PROPAGATE       if set to 1, run the app command bare (no opentelemetry-
#                      instrument wrapper): trace context stops flowing THROUGH
#                      the app — and nothing more (the sidecar still emits).
#                      For a baseline/uninstrumented run. (Alias: NO_OTEL.)
#   NO_EMIT            if set to 1, omit the lineage-telemetry plugin from the
#                      generated ConfigMap: the sidecar emits zero spans — and
#                      nothing more (it still proxies; parsers stay — legal
#                      alone; the plugin declares RequiresAny{parsers}, not the
#                      reverse). NO_PROPAGATE=1 NO_EMIT=1 together = lineage
#                      fully off for this app (see lineage-switch.sh).
#                      (Alias: NO_LINEAGE.)
#   EMIT               what to emit (this script is the ONE source of every
#                      lineage YAML byte):
#                        manifest  (default) ConfigMap + Service + Deployment
#                        cm        the per-app lineage ConfigMap alone
#                        patch     a strategic-merge patch adding the sidecar
#                                  pieces to an EXISTING Deployment (used by
#                                  sidecar-patch.sh; app container untouched)
set -euo pipefail

EMIT="${EMIT:-manifest}"
NAME="${NAME:?set NAME}"
case "$EMIT" in
  manifest) IMAGE="${IMAGE:?set IMAGE}" ;;
  cm|patch) IMAGE="${IMAGE:-}" ;;
  *) echo "error: EMIT must be manifest|cm|patch (got '$EMIT')" >&2; exit 2 ;;
esac
SELF_ID="${SELF_ID:-$NAME}"
APP_PORT="${APP_PORT:-8000}"
SVC_PORT="${SVC_PORT:-8080}"
APP_ENTRYPOINT="${APP_ENTRYPOINT:-server}"
ENV_VARS="${ENV_VARS:-}"
OUTBOUND_PORTS_EXCLUDE="${OUTBOUND_PORTS_EXCLUDE:-}"
PVC_NAME="${PVC_NAME:-}"
PVC_MOUNT="${PVC_MOUNT:-}"
NAMESPACE="${NAMESPACE:-team1}"
OTEL_ENDPOINT="${OTEL_ENDPOINT:-otel-collector.kagenti-system.svc.cluster.local:4317}"
KAGENTI_TYPE="${KAGENTI_TYPE:-agent}"
# Each toggle kills exactly one layer and nothing more; the old names are
# accepted as aliases (new name wins if both are set).
NO_PROPAGATE="${NO_PROPAGATE:-${NO_OTEL:-0}}"
NO_EMIT="${NO_EMIT:-${NO_LINEAGE:-0}}"

# ---- build the container command array (YAML flow sequence) ----
otel_prefix='"uv","run","--no-sync","opentelemetry-instrument","--traces_exporter","none","--metrics_exporter","none","--logs_exporter","none"'
app_tokens=""
for tok in $APP_ENTRYPOINT; do app_tokens="${app_tokens}\"${tok}\","; done
app_tokens="${app_tokens%,}"
if [ "$NO_PROPAGATE" = "1" ]; then
  CMD_ARRAY="[${app_tokens}]"
else
  CMD_ARRAY="[${otel_prefix},${app_tokens}]"
fi

# ---- build env block ----
# Exporter suppression lives ONLY in the command wrapper's --*_exporter none
# flags (the launcher writes its flags over the environment, so an env copy is
# dead weight — and under NO_PROPAGATE=1 it could clobber an instrumented app's own
# exporter config). Env carries only what the wrapper actually reads and the
# flags don't cover: propagators + service name.
env_block=$(cat <<EOF
            - { name: OTEL_PROPAGATORS,      value: "tracecontext,baggage" }
            - { name: PORT, value: "${APP_PORT}" }
            - { name: HOST, value: "0.0.0.0" }
            # HOME=/tmp + UV_NO_CACHE: uv run --no-sync execs from the existing
            # venv and needs no cache; disabling the cache entirely sidesteps
            # every cache-dir-not-writable-for-UID-1001 variant (images chown
            # /app differently, and some pre-seed a root-owned /tmp/uv-cache).
            - { name: HOME, value: "/tmp" }
            - { name: UV_NO_CACHE, value: "1" }
EOF
)
# OTEL_SERVICE_NAME defaults to SELF_ID; skip the default when the caller
# supplies one via ENV_VARS so the caller's value wins (no duplicate entry).
case " $ENV_VARS" in
  *" OTEL_SERVICE_NAME="*) ;;
  *) env_block="${env_block}
            - { name: OTEL_SERVICE_NAME,     value: \"${SELF_ID}\" }" ;;
esac
for kv in $ENV_VARS; do
  k="${kv%%=*}"; v="${kv#*=}"
  env_block="${env_block}
            - { name: ${k}, value: \"${v}\" }"
done

# ---- proxy-init OUTBOUND_PORTS_EXCLUDE env (only if set) ----
exclude_env=""
if [ -n "$OUTBOUND_PORTS_EXCLUDE" ]; then
  exclude_env="
            - name: OUTBOUND_PORTS_EXCLUDE
              value: \"${OUTBOUND_PORTS_EXCLUDE}\""
fi

# ---- optional shared PVC mounted into the app container (only if set) ----
# `kubectl apply` of the same PVC from several rows is idempotent — the claim
# is shared BY DESIGN (cross-session writer + reader pods on single-node kind).
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

# ---- shared fragments (identical bytes in EMIT=manifest and EMIT=patch) ----

sidecar_container() {  # the envoy-proxy container (8-space list-item indent)
  cat <<EOF
        # lineage sidecar: Envoy + authbridge-envoy(ext_proc + lineage plugin).
        # MUST run as UID 1337 (excluded from the iptables outbound redirect).
        - name: envoy-proxy
          image: docker.io/library/authbridge-envoy:latest
          imagePullPolicy: IfNotPresent
          args: ["--config", "/etc/authbridge/config.yaml"]
          securityContext:
            runAsNonRoot: false
            runAsUser: 1337
            runAsGroup: 1337
          ports:
            - { containerPort: 15123, name: envoy-out }
            - { containerPort: 15124, name: envoy-in }
            - { containerPort: 9090,  name: ext-proc }
            - { containerPort: 9901,  name: envoy-admin }
          volumeMounts:
            - { name: envoy-config,       mountPath: /etc/envoy }
            - { name: authbridge-runtime, mountPath: /etc/authbridge }
EOF
}

proxy_init_container() {  # the iptables init container
  cat <<EOF
        - name: proxy-init
          image: docker.io/library/proxy-init:latest
          imagePullPolicy: IfNotPresent
          securityContext:
            runAsNonRoot: false
            runAsUser: 0
            capabilities:
              add: ["NET_ADMIN"]
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
    # as anonymous http, which the DG UI then hides as infrastructure.
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
    app.kubernetes.io/name: ${NAME}
    kagenti.io/type: ${KAGENTI_TYPE}
    protocol.kagenti.io/a2a: ""
spec:
  ports:
    - name: http
      port: ${SVC_PORT}
      protocol: TCP
      targetPort: ${APP_PORT}
  selector:
    app.kubernetes.io/name: ${NAME}
    kagenti.io/type: ${KAGENTI_TYPE}
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
    app.kubernetes.io/name: ${NAME}
    kagenti.io/type: ${KAGENTI_TYPE}
spec:
  replicas: 1
  selector:
    matchLabels:
      app.kubernetes.io/name: ${NAME}
      kagenti.io/type: ${KAGENTI_TYPE}
  template:
    metadata:
      labels:
        app.kubernetes.io/name: ${NAME}
        kagenti.io/type: ${KAGENTI_TYPE}
        protocol.kagenti.io/a2a: ""
        # keep the operator's (broken, SPIRE-mounting) auto-injection OFF —
        # we inject our own sidecar below.
        kagenti.io/inject: disabled
    spec:
      containers:
        - name: agent
          image: ${IMAGE}
          imagePullPolicy: IfNotPresent
          command: ${CMD_ARRAY}
          ports:
            - { containerPort: ${APP_PORT}, name: http }
          env:
${env_block}
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

case "$EMIT" in
  cm)    emit_configmap ;;
  patch) emit_patch ;;
  manifest) cat <<EOF
# GENERATED by demos/lineage-adapter/attach-lineage.sh — do not hand-edit; re-run.
# app=${NAME} self_id=${SELF_ID} image=${IMAGE} propagate=$([ "$NO_PROPAGATE" = 1 ] && echo off || echo on) emit=$([ "$NO_EMIT" = 1 ] && echo off || echo on)${pvc_manifest}
---
$(emit_configmap)
---
$(emit_service)
---
$(emit_deployment)
EOF
  ;;
esac
