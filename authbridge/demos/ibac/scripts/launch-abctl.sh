#!/bin/bash
# Launch abctl against the IBAC agent's authbridge sidecar.
#
# Since abctl now ships with a built-in Namespaces → Pods picker that
# spawns its own kubectl port-forward, this script is a thin wrapper
# that just verifies prerequisites and runs the binary. The picker
# discovers the agent automatically; the user picks team1/email-agent.

set -euo pipefail

NAMESPACE=${1:-team1}
AGENT_NAME=${2:-email-agent}
ABCTL_BIN=${ABCTL_BIN:-/tmp/abctl-ibac-demo}

# 1. The Makefile's build-abctl target builds the binary on demand.
#    If it's still missing, the user invoked the script directly —
#    point them at the right entry point.
if [[ ! -x "$ABCTL_BIN" ]]; then
  echo "ERROR: abctl binary not found at $ABCTL_BIN." >&2
  echo "       Run \`make show-result\` (which depends on build-abctl)" >&2
  echo "       or \`make build-abctl\` first." >&2
  exit 1
fi

# 2. Verify the agent pod is up — friendlier failure than dropping the
#    user into an empty picker.
if ! kubectl -n "$NAMESPACE" get deploy "$AGENT_NAME" >/dev/null 2>&1; then
  echo "ERROR: deployment $NAMESPACE/$AGENT_NAME not found." >&2
  echo "       Run 'make demo-ibac' first." >&2
  exit 1
fi

# 3. Run abctl. The picker handles port-forward setup + teardown.
"$ABCTL_BIN"
