# shellcheck shell=bash
# 00-discover-keycloak.sh — set up the environment the uc1-onboarding demo needs.
#
#   source init/00-discover-keycloak.sh     # from demo/use-cases/uc1-onboarding/
#   source ./00-discover-keycloak.sh        # from within init/
#
# Exports KEYCLOAK_URL / KEYCLOAK_ADMIN_USERNAME / KEYCLOAK_ADMIN_PASSWORD and
# port-forwards the in-cluster keycloak-service to a local port so the demo's
# `make prereqs` / `make clear` (and every other target) can reach Keycloak.
#
# MUST be sourced, not executed — a child process cannot export vars back into
# your shell. It leaves a background `kubectl port-forward` running; its PID is
# stashed in $AIAC_KC_PF_PID. Tear it down with:  kill "$AIAC_KC_PF_PID"
#
# Overridable knobs (all have sane defaults):
#   KC_NAMESPACE   (keycloak)                namespace holding keycloak-service
#   KC_SERVICE     (svc/keycloak-service)    port-forward target
#   KC_REMOTE_PORT (8080)                     service port
#   KC_LOCAL_PORT  (18080)                    local port (8080 is usually taken
#                                             by the kind/rootless node)
#   KC_SECRET_NS   (aiac-system)             namespace holding the admin secret
#   KC_SECRET      (keycloak-admin-secret)   secret with the admin creds

# --- guard: refuse to run as a subprocess ------------------------------------
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  echo "ERROR: source this script — don't execute it:  source init/00-discover-keycloak.sh" >&2
  exit 1
fi

# Wrapped in a function so we can `return` on error without killing the shell.
_aiac_load_env() {
  local ns="${KC_NAMESPACE:-keycloak}"
  local svc="${KC_SERVICE:-svc/keycloak-service}"
  local rport="${KC_REMOTE_PORT:-8080}"
  local lport="${KC_LOCAL_PORT:-18080}"
  local secret_ns="${KC_SECRET_NS:-aiac-system}"
  local secret="${KC_SECRET:-keycloak-admin-secret}"
  local url="http://localhost:${lport}"
  local wellknown="${url}/realms/master/.well-known/openid-configuration"

  command -v kubectl >/dev/null 2>&1 || { echo "ABORT: kubectl not on PATH" >&2; return 1; }
  kubectl cluster-info >/dev/null 2>&1 || { echo "ABORT: kubectl cannot reach a cluster" >&2; return 1; }

  # Reuse an existing reachable forward if one is already up on this port.
  if curl -sf -o /dev/null --max-time 3 "$wellknown"; then
    echo "  Keycloak already reachable at ${url} — reusing it"
  else
    echo "  Starting port-forward ${ns}/${svc} ${lport}:${rport} ..."
    kubectl port-forward -n "$ns" "$svc" "${lport}:${rport}" >/dev/null 2>&1 &
    export AIAC_KC_PF_PID=$!
    # Wait for readiness (up to ~30s).
    local i
    for i in $(seq 1 30); do
      curl -sf -o /dev/null --max-time 2 "$wellknown" && break
      # If the port-forward died, stop waiting.
      kill -0 "$AIAC_KC_PF_PID" 2>/dev/null || { echo "ABORT: port-forward exited early" >&2; return 1; }
      sleep 1
    done
    if ! curl -sf -o /dev/null --max-time 2 "$wellknown"; then
      echo "ABORT: Keycloak not reachable at ${url} after 30s" >&2
      kill "$AIAC_KC_PF_PID" 2>/dev/null
      unset AIAC_KC_PF_PID
      return 1
    fi
    echo "  Port-forward ready (pid ${AIAC_KC_PF_PID})"
  fi

  # Pull admin creds straight from the secret (never echoed).
  local user pass
  user="$(kubectl get secret "$secret" -n "$secret_ns" -o jsonpath='{.data.KEYCLOAK_ADMIN_USERNAME}' 2>/dev/null | base64 -d)"
  pass="$(kubectl get secret "$secret" -n "$secret_ns" -o jsonpath='{.data.KEYCLOAK_ADMIN_PASSWORD}' 2>/dev/null | base64 -d)"
  if [[ -z "$user" || -z "$pass" ]]; then
    echo "ABORT: could not read KEYCLOAK_ADMIN_USERNAME/PASSWORD from secret ${secret_ns}/${secret}" >&2
    return 1
  fi

  export KEYCLOAK_URL="$url"
  export KEYCLOAK_ADMIN_USERNAME="$user"
  export KEYCLOAK_ADMIN_PASSWORD="$pass"

  echo "  KEYCLOAK_URL=${KEYCLOAK_URL}"
  echo "  KEYCLOAK_ADMIN_USERNAME=${KEYCLOAK_ADMIN_USERNAME}"
  echo "  KEYCLOAK_ADMIN_PASSWORD=******** (set)"
  echo "Environment ready. Run:  make prereqs   /   make clear"
}

_aiac_load_env
