#!/usr/bin/env python3
"""Print the shape of one trace from the platform collector's debug log.

The stock collector prints every span it receives (debug exporter,
verbosity: detailed). This reads that log, keeps the sidecar's spans for one
trace id and lists them in time order — self id, direction, protocol, role,
where the parent came from, the peer — then says whether the shape is right:
one unstamped hop at the entry (an inbound: `wire` when the caller sent a
`traceparent`, `none` when it sent nothing), `tracestate` everywhere else, and
the app's own outbound calls *in this trace*. An entry alone is not a good
shape: it means the app's calls went to traces of their own, and those are
counted too.

Usage: ./show-trace.py <trace-id> [--since 10m]
"""

import argparse
import collections
import re
import subprocess
import sys

ATTR = re.compile(r"-> ([\w.]+): Str\((.*)\)$", re.M)
TRACE = re.compile(r"Trace ID\s*:\s*(\w+)")
START = re.compile(r"Start time\s*:\s*(\S+ \S+)")


def sidecar_blocks(log: str):
    for block in re.split(r"\n(?=Span #\d+)", log):
        if "lineage.role" in block:
            yield block


def stray_outbound_traces(log: str, trace_id: str, first: str, last: str) -> int:
    """Traces other than trace_id that begin with an outbound hop that had no
    stamp to parent on (`wire` or `none`) — an app call that started a trace
    of its own — started while trace_id was in flight (between its first and
    last span)."""
    strays = set()
    for block in sidecar_blocks(log):
        attrs = dict(ATTR.findall(block))
        tid = TRACE.search(block)
        start = START.search(block)
        if (
            tid
            and start
            and tid.group(1) != trace_id
            and first <= start.group(1) <= last
            and attrs.get("lineage.role") == "request"
            and attrs.get("lineage.direction") == "outbound"
            and attrs.get("lineage.parent.source") in ("wire", "none")
        ):
            strays.add(tid.group(1))
    return len(strays)


def spans_for(log: str, trace_id: str):
    rows = []
    for block in sidecar_blocks(log):
        tid = TRACE.search(block)
        if not tid or tid.group(1) != trace_id:
            continue
        attrs = dict(ATTR.findall(block))
        start = START.search(block)
        rows.append(
            (
                start.group(1) if start else "",  # full timestamp: sorts across midnight
                attrs.get("lineage.self.id", ""),
                attrs.get("lineage.direction", ""),
                attrs.get("lineage.protocol", ""),
                attrs.get("lineage.role", ""),
                attrs.get("lineage.parent.source", ""),
                attrs.get("lineage.peer.host", "")[:30],
                attrs.get("lineage.outcome", ""),
            )
        )
    rows.sort()
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("trace_id")
    ap.add_argument("--since", default="10m", help="collector log window (kubectl --since)")
    ap.add_argument("--namespace", default="rossoctl-system")
    args = ap.parse_args()
    log = subprocess.run(  # noqa: S603 — fixed argv, no shell
        ["kubectl", "-n", args.namespace, "logs", "deploy/otel-collector", "--since", args.since],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    rows = spans_for(log, args.trace_id)
    if not rows:
        print(f"no sidecar spans for {args.trace_id} in the last {args.since}", file=sys.stderr)
        return 1
    print(f"{'time':<13}{'self':<17}{'dir':<10}{'proto':<11}{'role':<10}{'parent':<12}{'peer':<31}outcome")
    for r in rows:
        print(f"{r[0][11:23]:<13}{r[1]:<17}{r[2]:<10}{r[3]:<11}{r[4]:<10}{r[5]:<12}{r[6]:<31}{r[7]}")
    requests = [r for r in rows if r[4] == "request"]
    parents = collections.Counter(r[5] for r in requests)
    by_proto = collections.Counter(f"{r[2]} {r[3]}" for r in requests)
    strays = stray_outbound_traces(log, args.trace_id, rows[0][0], rows[-1][0])
    print()
    mix = ", ".join(f"{n} {k}" for k, n in sorted(by_proto.items()))
    print(f"{len(rows)} sidecar spans, {len(requests)} exchanges: {mix}")
    wire, stamped, none = parents.get("wire", 0), parents.get("tracestate", 0), parents.get("none", 0)
    print(f"parent.source: {wire} wire, {stamped} tracestate, {none} none")
    print(f"traces begun by an unparented outbound hop while this one was in flight: {strays}")
    has_outbound = any(r[2] == "outbound" for r in requests)
    # The one unstamped hop must be the first request AND an inbound: an
    # unstamped outbound root is a stray trace, whatever else it holds.
    unstamped = parents.get("wire", 0) + parents.get("none", 0)
    one_root = unstamped == 1 and requests[0][5] in ("wire", "none") and requests[0][2] == "inbound"
    if one_root and has_outbound and strays == 0:
        print("shape: OK — one root, unstamped only at the entry, the app's calls are in this trace")
        return 0
    if not has_outbound:
        print("shape: ENTRY ONLY — nothing the app called landed here; its calls are the stray traces above")
    else:
        print("shape: FRAGMENTED — an unstamped non-entry hop, or stray traces, mark un-propagated calls")
    return 2


if __name__ == "__main__":
    sys.exit(main())
