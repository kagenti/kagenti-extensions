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
# NOTE: natively-instrumented apps (weather_service/weather_tool export their
# own OTLP) need no shim — in-process context already propagates. Apps that are
# NOT instrumented still need the shim for correct pairing under concurrency;
# for those, prefer a fleet.conf row + deploy-fleet.sh (see RUNBOOK.md).
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

DEPLOY="${DEPLOY:?usage: DEPLOY=<deployment> [NAMESPACE=team1] [SELF_ID=<id>] [OUTBOUND_PORTS_EXCLUDE=ports] sidecar-patch.sh}"
NAMESPACE="${NAMESPACE:-team1}"
SELF_ID="${SELF_ID:-$DEPLOY}"
OTEL_ENDPOINT="${OTEL_ENDPOINT:-otel-collector.kagenti-system.svc.cluster.local:4317}"
OUTBOUND_PORTS_EXCLUDE="${OUTBOUND_PORTS_EXCLUDE:-}"

kubectl get deploy -n "$NAMESPACE" "$DEPLOY" >/dev/null
kubectl get cm -n "$NAMESPACE" envoy-config >/dev/null || {
  echo "error: ConfigMap envoy-config missing in $NAMESPACE (platform-rendered by the kagenti chart)" >&2
  exit 1
}

# ---- 1) per-app lineage config ----
# Pipeline content MUST stay in sync with attach-lineage.sh (the canonical
# emitter): uniform parser chain in both directions — see the rationale comment
# there. Only the delivery differs (standalone CM here vs emitted manifest).
kubectl apply -f - <<EOF
apiVersion: v1
kind: ConfigMap
metadata:
  name: authbridge-lineage-config-${DEPLOY}
  namespace: ${NAMESPACE}
data:
  config.yaml: |
    mode: envoy-sidecar
    pipeline:
      inbound:
        plugins:
          - name: a2a-parser
          - name: mcp-parser
          - name: inference-parser
          - name: lineage-telemetry
            config:
              otel_endpoint: "${OTEL_ENDPOINT}"
              capture_io: true
              self_id: "${SELF_ID}"
      outbound:
        plugins:
          - name: a2a-parser
          - name: mcp-parser
          - name: inference-parser
          - name: lineage-telemetry
            config:
              otel_endpoint: "${OTEL_ENDPOINT}"
              capture_io: true
              self_id: "${SELF_ID}"
EOF

# ---- 2) strategic-merge patch: ADD proxy-init + envoy-proxy + volumes ----
exclude_env=""
if [ -n "$OUTBOUND_PORTS_EXCLUDE" ]; then
  exclude_env="
            - name: OUTBOUND_PORTS_EXCLUDE
              value: \"${OUTBOUND_PORTS_EXCLUDE}\""
fi

kubectl patch deploy "$DEPLOY" -n "$NAMESPACE" --type strategic --patch "
spec:
  template:
    spec:
      initContainers:
        - name: proxy-init
          image: docker.io/library/proxy-init:latest
          imagePullPolicy: IfNotPresent
          securityContext:
            runAsNonRoot: false
            runAsUser: 0
            capabilities:
              add: [\"NET_ADMIN\"]
          env:
            - name: POD_IP
              valueFrom:
                fieldRef:
                  fieldPath: status.podIP${exclude_env}
      containers:
        # Envoy + authbridge-envoy (ext_proc + lineage plugin). MUST run as
        # UID 1337 — init-iptables excludes 1337 from the outbound redirect.
        - name: envoy-proxy
          image: docker.io/library/authbridge-envoy:latest
          imagePullPolicy: IfNotPresent
          args: [\"--config\", \"/etc/authbridge/config.yaml\"]
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
      volumes:
        - name: envoy-config
          configMap:
            name: envoy-config
        - name: authbridge-runtime
          configMap:
            name: authbridge-lineage-config-${DEPLOY}
            items:
              - { key: config.yaml, path: config.yaml }
"

kubectl rollout status -n "$NAMESPACE" "deploy/$DEPLOY" --timeout=180s
echo ">> lineage sidecar attached to deploy/$DEPLOY (self_id=$SELF_ID, ns=$NAMESPACE)"
