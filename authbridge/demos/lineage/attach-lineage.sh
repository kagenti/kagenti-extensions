#!/usr/bin/env bash
# Given a Deployment's NAME (and, optionally, which of its containers to switch
# propagation on and with which image), print ONE of the two objects that
# attach lineage to it: the sidecar's config (EMIT=cm) or the strategic-merge
# patch that bolts the sidecar onto the Deployment (EMIT=patch). Apply both
# and the app's traffic flows through the lineage plugin.
#
# Lineage attachment generator. Emits (to stdout) the YAML that attaches the
# AuthBridge lineage sidecar to an EXISTING Deployment — and nothing else. It
# does not create, own or describe the application: no Deployment, no Service,
# no app configuration. The app is deployed however its owner deploys apps;
# this script produces only the lineage-related additions:
#
#   EMIT=patch (default)  a strategic-merge patch adding the sidecar pieces
#                         (proxy-init initContainer, envoy-proxy container,
#                         the two config volumes) to a Deployment. Lists merge
#                         by name, so nothing already in the Deployment is
#                         touched. With APP_CONTAINER set, the patch also
#                         flips the propagation switch on the app's own
#                         container (see below).
#   EMIT=cm               the per-app plugin ConfigMap the sidecar mounts
#                         (parser chain + lineage-telemetry entry).
#
# Two ways to consume the output:
#   live    sidecar-patch.sh applies both against a running Deployment
#           (with preconditions checked); the owner keeps owning the object.
#   source  commit both outputs next to your own manifests and list the patch
#           in a kustomization (`patches: - path: lineage-patch.yaml` with
#           target kind Deployment / name <NAME>) — the attachment then lives
#           in version control and survives every re-deploy.
#
# Propagation (the shim half): capture alone cannot attribute an app's
# outbound calls to the inbound that caused them — the app must carry
# `traceparent` through itself. For an uninstrumented Python app, bake the
# propagate-only shim onto its image first (build-otel-shim.sh) and then let
# this patch activate it: APP_CONTAINER names the app's container and the
# patch sets LINEAGE_PROPAGATE=1 in its env (and, when APP_IMAGE is given,
# points the container at the baked -otel image). Without APP_CONTAINER the
# patch is capture-only and the app container is not touched at all.
#
# Usage:
#   NAME=echo-upstream ./attach-lineage.sh                     # the patch
#   NAME=echo-upstream EMIT=cm ./attach-lineage.sh             # the ConfigMap
#   NAME=my-agent APP_CONTAINER=agent \
#     APP_IMAGE=docker.io/library/my-agent-otel:latest \
#     ./attach-lineage.sh                                      # patch + propagation
#
# Variables:
#   NAME               (required) the target Deployment's name; also names the
#                      generated ConfigMap (authbridge-lineage-config-<NAME>)
#                      and defaults SELF_ID.
#   NAMESPACE          (default team1, the platform's demo namespace).
#   SELF_ID            lineage self_id (default: NAME). Only this varies in
#                      the plugin config.
#   OTEL_ENDPOINT      OTLP/gRPC endpoint the lineage plugin exports to (default
#                      otel-collector.rossoctl-system.svc.cluster.local:4317).
#                      Any OTLP consumer works — Phoenix and Jaeger included;
#                      nothing downstream of this endpoint is assumed.
#   APP_CONTAINER      optional: the app container's name in the target
#                      Deployment. When set, the patch adds
#                      LINEAGE_PROPAGATE=1 to that container's env (merged by
#                      name — its other env entries are untouched). MUST match
#                      an existing container: a strategic merge ADDS a new
#                      stub container for an unknown name rather than failing.
#                      This script cannot check that (it never touches the
#                      cluster); sidecar-patch.sh does before applying.
#   APP_IMAGE          optional, needs APP_CONTAINER: image ref to set on the
#                      app container — the -otel image build-otel-shim.sh
#                      produced from the container's current image.
#   OUTBOUND_PORTS_EXCLUDE  iptables outbound excludes (default ''). Set to an
#                      app's OWN OTLP export port (e.g. 4317/4318) for an app that
#                      already exports spans, so that export keeps flowing
#                      untouched. Do NOT exclude LLM/tool ports — we want those seen.
#   SIDECAR_IMAGE      envoy+authbridge sidecar image (default
#                      ghcr.io/rossoctl/cortex/authbridge-envoy:latest).
#                      UNTIL A RELEASE CARRIES THE lineage-telemetry PLUGIN
#                      the published default boots without it (the sidecar
#                      logs `unknown plugin "lineage-telemetry"`): build the
#                      image from this repo and point this at your tag.
#   PROXY_INIT_IMAGE   iptables init image (default
#                      ghcr.io/rossoctl/cortex/proxy-init:latest); build it
#                      alongside SIDECAR_IMAGE when building from source.
#   NO_EMIT            if set to 1, omit the lineage-telemetry plugin from the
#                      generated ConfigMap: the sidecar emits zero spans — and
#                      nothing more (it still proxies; parsers stay — legal
#                      alone; the plugin declares RequiresAny{parsers}, not the
#                      reverse). For a baseline run.
#   EMIT               patch (default) | cm — see above.
#
# Structure: main() at the bottom is the pipeline — parse_inputs reads,
# defaults and validates EVERY knob (all refusals live there; nothing after
# it can reject), the build_* functions each assemble one YAML fragment into
# a global, and emit() dispatches to the two emitters. The three fragment
# functions (sidecar_container / proxy_init_container / sidecar_volumes) are
# the single source of the sidecar YAML.
set -euo pipefail

# ---- input guard for caller-supplied values ----
# Every free-form caller value (SELF_ID, OTEL_ENDPOINT, the three image refs)
# lands in the generated YAML inside a double-quoted scalar, where only '"'
# and '\' are special — so refusing those two is exactly sufficient for the
# output to parse back to the caller's string. Whitespace is refused too: it
# is never part of an endpoint or image ref, so it can only be a mistake.
# The exclude list is integers separated by commas (what proxy-init accepts).
yaml_safe() {  # $1 = what it is (for the error), $2 = the value
  local unsafe=$'"\\'
  case "$2" in
    *["$unsafe"]*|*[[:space:]]*)
      printf "error: %s '%s' contains whitespace or one of %s, which this script cannot quote safely\n" "$1" "$2" "$unsafe" >&2
      exit 2 ;;
  esac
}

parse_inputs() {
  # Knobs of the removed deploy-the-app-yourself mode (EMIT=manifest). A caller
  # still passing one is running a stale recipe — refuse so the difference is
  # loud, not silent. This script attaches lineage; it does not deploy apps.
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
  # NAME becomes object names, so it must be a lowercase RFC 1123 name;
  # NAMESPACE is a DNS label. Both are then safe everywhere they are
  # interpolated bare.
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
  # Published images by default so the demo runs against a stock platform; point
  # these at locally-built tags when you build the sidecar from source.
  SIDECAR_IMAGE="${SIDECAR_IMAGE:-ghcr.io/rossoctl/cortex/authbridge-envoy:latest}"
  PROXY_INIT_IMAGE="${PROXY_INIT_IMAGE:-ghcr.io/rossoctl/cortex/proxy-init:latest}"
  NO_EMIT="${NO_EMIT:-0}"

  local v
  for v in SELF_ID OTEL_ENDPOINT APP_IMAGE SIDECAR_IMAGE PROXY_INIT_IMAGE; do
    yaml_safe "$v" "${!v}"
  done
  # A container name is a DNS label (it is matched by name in the strategic
  # merge — a wrong name silently patches nothing, but a malformed one would
  # emit broken YAML).
  if [ -n "$APP_CONTAINER" ] && ! [[ "$APP_CONTAINER" =~ ^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$ ]]; then
    echo "error: APP_CONTAINER='$APP_CONTAINER' is not a valid container name (DNS label)" >&2; exit 2
  fi
  if [ -n "$APP_IMAGE" ] && [ -z "$APP_CONTAINER" ]; then
    echo "error: APP_IMAGE needs APP_CONTAINER — the image lands on a container the patch must name" >&2; exit 2
  fi
  [[ "$OUTBOUND_PORTS_EXCLUDE" =~ ^([0-9]+(,[0-9]+)*)?$ ]] \
    || { echo "error: OUTBOUND_PORTS_EXCLUDE='$OUTBOUND_PORTS_EXCLUDE' is not a comma-separated port list" >&2; exit 2; }
  local port
  for port in ${OUTBOUND_PORTS_EXCLUDE//,/ }; do
    if [ "$port" -lt 1 ] || [ "$port" -gt 65535 ]; then
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

build_app_patch() {
  # The propagation switch on the app's own container, opt-in via
  # APP_CONTAINER. Strategic merge keys both `containers` and `env` entries by
  # name, so this adds/updates exactly LINEAGE_PROPAGATE (and the image when
  # APP_IMAGE is given) and leaves everything else of the container alone.
  # The whole switch is ONE env var: it wakes the hook baked into the -otel
  # image (build-otel-shim.sh), which pins the propagate-only posture itself
  # (all exporters none, tracecontext+baggage propagators — as env DEFAULTS,
  # so a deliberate override in the Deployment still wins).
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
        # lineage sidecar: Envoy + authbridge-envoy(ext_proc + lineage plugin).
        # MUST run as UID 1337 (excluded from the iptables outbound redirect).
        # Only the three ports the pod needs are declared; the Envoy admin
        # port binds loopback and nothing here reads it. Readiness is the
        # inbound listener accepting: without it a Service endpoint goes
        # Ready before the sidecar can take the redirected connection.
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
        # iptables needs root + NET_ADMIN/NET_RAW (proxy-init's README) and
        # nothing else: everything is dropped first, then exactly those two
        # are added back.
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

emit_patch() {  # strategic merge: lists merge by name — nothing already in the Deployment is touched
  # apiVersion/kind/metadata make the patch a complete resource: kustomize
  # refuses a bare `spec:` fragment ("unable to parse SM or JSON patch"),
  # and `kubectl patch` merges the identifying fields harmlessly.
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
