#!/usr/bin/env bash
#
# Test harness for init-iptables.sh "enforce-redirect" mode (proxy-sidecar
# fail-closed egress guard, capture variant).
#
# It validates, in a private network namespace:
#   1. Rule STRUCTURE — the AB_REDIRECT chain is hooked from nat OUTPUT at
#      position 1 with the expected RETURN exemptions and a `-p tcp` REDIRECT to
#      TRANSPARENT_PORT (no DROP — the nat table forbids it); and the AB_NOTCP
#      chain is hooked from mangle OUTPUT with `-p tcp RETURN` then a terminal
#      DROP for external non-TCP egress.
#   2. CAPTURE (not drop) + AMBIENT ROBUSTNESS — external TCP egress is
#      REDIRECTed to TRANSPARENT_PORT, preempting a simulated Istio ambient
#      "nat OUTPUT REDIRECT" appended after our chain. Proven via packet
#      counters: our REDIRECT increments, the simulated ISTIO REDIRECT does not.
#   3. NON-TCP DROP — an external UDP datagram (QUIC/HTTP-3 bypass attempt) hits
#      the mangle AB_NOTCP DROP, proving non-TCP external egress cannot bypass.
#
# Requirements: root (for unshare --net + iptables), iproute2, iptables-nft,
# bash, the dummy kernel module. Runs on Linux / CI (e.g. ubuntu-latest); not on
# macOS. Uses `unshare --net` so it also works inside nested containers. Exit
# code 0 = all pass.
set -euo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
INIT="${INIT_SCRIPT:-${SCRIPT_DIR}/init-iptables.sh}"
IPT="${IPTABLES_CMD:-iptables-nft}"
EXTERNAL="198.51.100.7"   # RFC5737 TEST-NET-2, guaranteed unused
TPORT="8082"

# Re-exec into a private network namespace.
if [ -z "${_AB_NETNS_REEXEC:-}" ]; then
  exec unshare --net env _AB_NETNS_REEXEC=1 INIT_SCRIPT="${INIT}" \
       IPTABLES_CMD="${IPT}" bash "$0" "$@"
fi

fail=0

# Fresh netns: bring up lo and a dummy default route so packets to an external
# destination are actually generated and traverse the OUTPUT chain.
ip link set lo up
if ip link add eth-test type dummy 2>/dev/null; then
  ip addr add 10.255.255.2/24 dev eth-test
  ip link set eth-test up
  ip route add default via 10.255.255.1
else
  echo "WARN: dummy interface unavailable; capture packet may not be generated"
fi

echo "### Installing enforce-redirect rules"
env MODE=enforce-redirect PROXY_UID=1337 CLUSTER_CIDRS=10.0.0.0/8 \
    TRANSPARENT_PORT="${TPORT}" \
    IPTABLES_CMD="${IPT}" IP6TABLES_CMD=ip6tables-nft \
    sh "${INIT}" || { echo "FAIL: init script exited non-zero"; exit 1; }

natdump=$("${IPT}" -t nat -S)
mangledump=$("${IPT}" -t mangle -S)
echo "--- nat ruleset ---"; echo "${natdump}"
echo "--- mangle ruleset ---"; echo "${mangledump}"

assert() { if echo "$3" | grep -qE "$2"; then echo "PASS: $1"; else echo "FAIL: $1"; fail=1; fi; }
# nat AB_REDIRECT — TCP capture (no DROP; nat forbids it).
assert "AB_REDIRECT hooked from nat OUTPUT" '^-A OUTPUT -j AB_REDIRECT' "${natdump}"
assert "nat ztunnel mark RETURN"            'AB_REDIRECT .*mark.*0x539.*-j RETURN' "${natdump}"
assert "nat proxy UID RETURN"               'AB_REDIRECT .*--uid-owner 1337 -j RETURN' "${natdump}"
assert "nat loopback iface RETURN"          'AB_REDIRECT -o lo -j RETURN' "${natdump}"
assert "nat loopback cidr RETURN"           'AB_REDIRECT -d 127.0.0.0/8 -j RETURN' "${natdump}"
assert "nat cluster cidr RETURN"            'AB_REDIRECT -d 10.0.0.0/8 -j RETURN' "${natdump}"
assert "nat tcp REDIRECT to transparent"    "AB_REDIRECT -p tcp -j REDIRECT --to-ports ${TPORT}" "${natdump}"
if echo "${natdump}" | grep -qE 'AB_REDIRECT -j DROP'; then
  echo "FAIL: nat AB_REDIRECT must not contain DROP (nat table forbids it)"; fail=1
else echo "PASS: nat AB_REDIRECT has no DROP (correctly delegated to mangle)"; fi
# mangle AB_NOTCP — non-TCP drop, TCP passes through to the nat REDIRECT.
assert "AB_NOTCP hooked from mangle OUTPUT"  '^-A OUTPUT -j AB_NOTCP' "${mangledump}"
assert "mangle established/related RETURN"   'AB_NOTCP -m conntrack --ctstate (ESTABLISHED,RELATED|RELATED,ESTABLISHED) -j RETURN' "${mangledump}"
assert "mangle proxy UID RETURN"             'AB_NOTCP .*--uid-owner 1337 -j RETURN' "${mangledump}"
assert "mangle cluster cidr RETURN"          'AB_NOTCP -d 10.0.0.0/8 -j RETURN' "${mangledump}"
assert "mangle tcp RETURN (defer to nat)"    'AB_NOTCP -p tcp -j RETURN' "${mangledump}"
assert "mangle terminal DROP (non-tcp)"      'AB_NOTCP -j DROP' "${mangledump}"

pos1=$("${IPT}" -t nat -L OUTPUT --line-numbers -n | awk '$1=="1"{print $2}')
if [ "${pos1}" = "AB_REDIRECT" ]; then echo "PASS: AB_REDIRECT at nat OUTPUT position 1"
else echo "FAIL: AB_REDIRECT not at nat OUTPUT position 1 (got '${pos1}')"; fail=1; fi
mpos1=$("${IPT}" -t mangle -L OUTPUT --line-numbers -n | awk '$1=="1"{print $2}')
if [ "${mpos1}" = "AB_NOTCP" ]; then echo "PASS: AB_NOTCP at mangle OUTPUT position 1"
else echo "FAIL: AB_NOTCP not at mangle OUTPUT position 1 (got '${mpos1}')"; fail=1; fi

echo "### Capture + preemption test: append a simulated ISTIO_OUTPUT nat REDIRECT"
"${IPT}" -t nat -A OUTPUT -p tcp -d "${EXTERNAL}" -j REDIRECT --to-ports 19999
# Generate an external TCP SYN (uid 0, like an agent bypass attempt). With no
# listener on TPORT the redirected SYN gets an RST; the rule counter still ticks.
timeout 2 bash -c "exec 3<>/dev/tcp/${EXTERNAL}/80" 2>/dev/null || true

capc=$("${IPT}" -t nat -L AB_REDIRECT -n -v | awk '/REDIRECT/{print $1; exit}')
istioc=$("${IPT}" -t nat -L OUTPUT -n -v | awk '/REDIRECT/{print $1; exit}')
echo "AB_REDIRECT REDIRECT pkts=${capc:-?} | simulated ISTIO REDIRECT pkts=${istioc:-?}"
if [ "${capc:-0}" -gt 0 ] && [ "${istioc:-0}" -eq 0 ]; then
  echo "PASS: external TCP captured to transparent port, preempting nat REDIRECT (ambient-robust)"
else
  echo "FAIL: capture/preemption not demonstrated (AB=${capc:-?}, ISTIO=${istioc:-?})"; fail=1
fi

echo "### Non-TCP drop test: send an external UDP datagram (QUIC bypass attempt)"
timeout 2 bash -c "echo -n x >/dev/udp/${EXTERNAL}/53" 2>/dev/null || true
dropc=$("${IPT}" -t mangle -L AB_NOTCP -n -v | awk '/DROP/{print $1; exit}')
echo "mangle AB_NOTCP DROP pkts=${dropc:-?}"
if [ "${dropc:-0}" -gt 0 ]; then
  echo "PASS: external UDP dropped (HTTP/3 cannot bypass)"
else
  echo "FAIL: external UDP not dropped (DROP=${dropc:-?})"; fail=1
fi

echo
[ "${fail}" -eq 0 ] && echo "ALL TESTS PASSED" || echo "SOME TESTS FAILED"
exit "${fail}"
