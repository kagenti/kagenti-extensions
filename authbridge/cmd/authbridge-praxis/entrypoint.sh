#!/bin/bash
set -eu

# AuthBridge praxis-sidecar combined entrypoint.
#
# Unlike the other AuthBridge entrypoints, this one is a two-phase pipeline
# rather than a process supervisor, because authbridge-praxis is a config
# GENERATOR, not a proxy:
#
#   Phase 1: authbridge-praxis — reads the AuthBridge config and writes a
#            Praxis proxy config (+ a Praxis policy document when the inbound
#            pipeline declares something the policy engine can enforce). Runs
#            to completion and exits.
#   Phase 2: praxis — the actual data plane, exec'd against the generated
#            config so it becomes PID 1.
#
# Phase 1 must succeed before phase 2 starts. `set -e` guarantees that: if
# authbridge-praxis exits non-zero (bad AuthBridge config, unconvertible
# pipeline, unwritable output path) the script aborts and the container exits
# non-zero, so Kubernetes restarts it. Starting Praxis anyway would serve
# traffic through a stale config from a previous run — or fail confusingly on
# a missing file — and in the inbound case that could mean proxying without
# the JWT validation the operator configured.
#
# Phase 2 uses `exec` so Praxis replaces this shell as PID 1. That gives it
# signals directly from the kubelet (it drains gracefully on SIGTERM) and
# leaves no supervisor to forward them. There is no third process to
# supervise, so the multi-process supervision the other entrypoints do would
# add nothing here.

# Where the generator writes, and Praxis reads. Overridable so the pair can be
# relocated together; the two MUST agree, which is why one variable feeds both
# the generator flag and the Praxis flag rather than being written twice.
PRAXIS_CONFIG="${PRAXIS_CONFIG:-/tmp/praxis-config.yaml}"
PRAXIS_POLICY="${PRAXIS_POLICY:-/tmp/praxis-policy.yaml}"

# The AuthBridge config to convert. Defaults to the same path the other
# AuthBridge images mount their runtime config at.
AUTHBRIDGE_CONFIG="${AUTHBRIDGE_CONFIG:-/etc/authbridge/config.yaml}"

# Where the inbound audience is read from when the jwt-validation plugin config
# names none literally. /shared/client-id.txt is the Rossoctl convention — the
# operator mounts the workload's Keycloak client ID there, and jwt-validation
# defaults to reading it — so this is the in-cluster shape.
#
# Passed only when the file actually exists. The generator treats an explicitly
# named but unreadable audience file as an error (a policy with no audience
# accepts any token from the issuer), which is right for a deliberate flag but
# wrong as an unconditional default: a config that states its audience inline
# needs no file, and standalone runs have no /shared mount at all.
AUDIENCE_FILE="${AUDIENCE_FILE:-/shared/client-id.txt}"

AUDIENCE_ARGS=""
if [ -s "${AUDIENCE_FILE}" ]; then
  echo "[entrypoint] Using ${AUDIENCE_FILE} as the inbound audience source"
  AUDIENCE_ARGS="--audience-file ${AUDIENCE_FILE}"
fi

# --- Phase 1: generate the Praxis config from the AuthBridge config ---
echo "[entrypoint] Generating Praxis config from ${AUTHBRIDGE_CONFIG}..."
# shellcheck disable=SC2086  # AUDIENCE_ARGS is intentionally word-split (empty = omitted)
/usr/local/bin/authbridge-praxis \
  --config "${AUTHBRIDGE_CONFIG}" \
  --praxis-config-out "${PRAXIS_CONFIG}" \
  --praxis-policy-out "${PRAXIS_POLICY}" \
  ${AUDIENCE_ARGS} \
  "$@"

# Fail loudly rather than handing Praxis a path that does not exist: the
# generator reports success by exiting 0, but a wrong --praxis-config-out or a
# read-only target would leave nothing behind, and `praxis -c` on a missing
# file is a much less obvious error than this one.
if [ ! -s "${PRAXIS_CONFIG}" ]; then
  echo "[entrypoint] ERROR: ${PRAXIS_CONFIG} was not written (or is empty); refusing to start Praxis" >&2
  exit 1
fi

echo "[entrypoint] Wrote ${PRAXIS_CONFIG}"
if [ -s "${PRAXIS_POLICY}" ]; then
  echo "[entrypoint] Wrote ${PRAXIS_POLICY} (policy engine will enforce it)"
else
  # Not an error: a pipeline with no jwt-validation has nothing for the policy
  # engine to enforce, and in that case the generated config carries no
  # `policy` filter either, so there is no dangling reference.
  echo "[entrypoint] No policy document generated (nothing in the inbound pipeline maps to a policy plugin)"
fi

# --- Phase 2: run Praxis against the generated config ---
# Validate before binding ports. Praxis checks filter ordering, cluster
# cross-references, and the policy document itself; catching a bad generated
# config here produces one clear error instead of a partially-bound proxy.
echo "[entrypoint] Validating generated config..."
/usr/local/bin/praxis --validate --config "${PRAXIS_CONFIG}"

echo "[entrypoint] Starting praxis with ${PRAXIS_CONFIG}..."
exec /usr/local/bin/praxis --config "${PRAXIS_CONFIG}"
