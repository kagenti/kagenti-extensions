#!/usr/bin/env bash
# verify-fleet.sh — drive every entry point in the fleet under concurrency and
# print the HARMONY TABLE: app -> correct inbound/outbound pairing (target N/N).
# A2A agents are driven via concurrency-test.sh; the wiki MCP front (2-service)
# via concurrency-test-mcp.sh. Downstream tools (slack-tool, reservation-tool)
# are exercised through their agent's cross-service call, not driven directly.
#
# Usage:
#   ./verify-fleet.sh                       # all entry points
#   ./verify-fleet.sh trivia-agent wiki     # only these
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
NAMESPACE="${NAMESPACE:-team1}"
N="${N:-6}"
SELECT=("$@")

# entry-point verification specs: name|kind|settle|payload
#   kind=a2a  -> payload is the PROMPT template ({TOKEN} substituted per request)
#   kind=mcp  -> payload is the MCP TOOL name (front=wiki-mcp, backend=wiki-service)
SPECS=(
  "trivia-agent|a2a|15|Ask one trivia question about the planet {TOKEN} only."
  "a2a-currency-converter|a2a|15|What is the exchange rate from USD to EUR? Reference code {TOKEN}."
  "a2a-contact-extractor|a2a|20|Extract the contact: Jane Doe, jane@example.com. Reference code {TOKEN}."
  "git-issue-agent|a2a|50|How do I file a good bug report? Keep it short. Reference code {TOKEN}."
  "slack-researcher|a2a|45|List the slack channels available. Reference code {TOKEN}."
  "reservation-service|a2a|45|Find restaurants in Boston for dinner. Reference code {TOKEN}."
  "wiki|mcp|15|wiki_query"
)

selected() { [ "${#SELECT[@]}" -eq 0 ] && return 0; local n; for n in "${SELECT[@]}"; do [ "$n" = "$1" ] && return 0; done; return 1; }

results=()
for spec in "${SPECS[@]}"; do
  IFS='|' read -r name kind settle payload <<< "$spec"
  selected "$name" || continue
  echo ""; echo "======== verifying $name ($kind) ========"
  out=""
  if [ "$kind" = "a2a" ]; then
    out=$(SELF_ID="$name" TARGET="${name}.${NAMESPACE}.svc.cluster.local:8080" \
          PROMPT="$payload" SETTLE="$settle" N="$N" NAMESPACE="$NAMESPACE" \
          "${SCRIPT_DIR}/concurrency-test.sh" 2>&1) || true
    score=$(printf '%s\n' "$out" | grep -oE 'CORRECT PAIRING: [0-9]+/[0-9]+' | tail -1 | grep -oE '[0-9]+/[0-9]+' || true)
  else
    out=$(FRONT=wiki-mcp BACKEND=wiki-service \
          MCP_URL="http://wiki-mcp.${NAMESPACE}.svc.cluster.local:8000/mcp" \
          TOOL="$payload" DRIVER_IMAGE=docker.io/library/wiki_memory_tool-otel:latest \
          SETTLE="$settle" N="$N" NAMESPACE="$NAMESPACE" \
          "${SCRIPT_DIR}/concurrency-test-mcp.sh" 2>&1) || true
    score=$(printf '%s\n' "$out" | grep -oE 'CORRECT 2-SERVICE PROPAGATION: [0-9]+/[0-9]+' | tail -1 | grep -oE '[0-9]+/[0-9]+' || true)
  fi
  printf '%s\n' "$out" | tail -3
  results+=("${name}|${score:-ERROR}")
done

echo ""
echo "================= HARMONY TABLE ================="
printf '%-26s %s\n' "ENTRY POINT" "PAIRING (target ${N}/${N})"
printf '%-26s %s\n' "--------------------------" "--------------------"
allpass=1
for r in "${results[@]}"; do
  IFS='|' read -r name score <<< "$r"
  mark="✓"; case "$score" in "${N}/${N}") ;; *) mark="✗"; allpass=0 ;; esac
  printf '%-26s %s  %s\n' "$name" "${score}" "$mark"
done
echo "================================================"
[ "$allpass" = "1" ] && echo "ALL IN HARMONY 🎶" || { echo "some entry points below target"; exit 1; }
