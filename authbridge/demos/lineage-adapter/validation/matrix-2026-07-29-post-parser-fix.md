# Validation record — full matrix re-run after the protocol-keyed payload fix (2026-07-29, evening)

One consolidated record for all 10 apps (per-app expectations carried over unchanged from the
`*-2026-07-29-post-cutover.md` cards; this run re-validates the producer after two fixes, so the
deltas — not the expectations — are the story).

## What changed since the T9 post-cutover cards

- **Producer fix `74765c6d`** (`fix(lineage): read payloads only through the protocol fact`):
  payload reads are keyed by the exchange's `lineage.protocol` fact; the co-populated MCP parse of
  an a2a body can no longer leak into `input.value`/`output.value`. Sidecar image rebuilt
  (`sha256:0fbd6ad54530…`), archive-loaded, full team1 fleet rolled. App images verified
  unchanged against the cards (trivia `8cf1958274…`, reservation `bb4d064615…`, slack-tool
  `db321ff4e59a…`).
- **Harness fix `c44a517c`**: `EXPECT_ROOTS` asserted per trace (from the card), plus the A2A
  forest-collapse guard (`roots==ix, ix>1` always fails).

## Result: 10/10 apps clean

DG image `d841c3999683` (unchanged), `INTERACTIONS_ALGORITHM=sidecar`, N=6 per app.

| app | harness result | shape (per trace) | roots |
|---|---|---|---|
| trivia-agent | 6/6 | ix=2 | 1 (asserted) |
| weather-service | 6/6 | ix=19 | 1 (asserted) |
| a2a-currency-converter | 6/6 | ix=3 | 1 (asserted) |
| a2a-contact-extractor | 6/6 | ix=5 | 1 (asserted) |
| git-issue-agent | 6/6 | ix=3 | 1 (asserted) |
| reservation-service | 6/6 | ix=32 | 16 (reported — see below) |
| slack pair | 6/6 | ix=20 | 7 (asserted) |
| wiki-mcp | 6/6 | ix=8 | 7 (asserted) |
| slack-tool | 6/6 | ix=6 | 6 (reported — see below) |
| reservation-tool | 6/6 | ix=5 | 5 (asserted) |

## The three first-pass failures — all harness-input artifacts, none pipeline defects

The first pass ran everything with `EXPECT_ROOTS` pinned from the cards and scored 7/10:

1. **weather 4/6** — kind pins missed `tool_call_*` on 2 traces: the review session's prompt
   ("What is the weather in {TOKEN}?") gave the LLM a random hex token as the location, and on 2
   of 6 turns qwen2.5:7b answered without calling the tool (session shape: initialize/tools-list
   only, ix=12). Re-run with a tool-forcing prompt: 6/6, ix=19, all 4 kinds + roots=1 asserted.
   Lesson: harness prompts for tool-wired apps must force the tool; the cards never recorded the
   prompt used. This record does: *"Use the weather tool to look up the current weather for the
   city code {TOKEN} and report exactly what the tool returns."*
2. **reservation 0/6 on `EXPECT_ROOTS=11`** — all 12 traces across both passes derived the OTHER
   documented shape, 32 ix / 16 roots (the T9 card itself records "22/11 vs the morning's 32/16";
   loop depth is the acknowledged variable). Structural invariants (entry=1, 0 orphans,
   anchors==ix, 0 dups, collapse guard) held 12/12. Roots left unpinned for this app; range
   recorded: **22/11 ↔ 32/16**.
3. **slack-tool 0/6 on `EXPECT_ROOTS=5`** — all 12 traces derived 6 exchanges (the extra one is
   the client session-close leg the T9 card documents as "5 vs the morning's 6"). Kinds
   (`tool_call_arguments=1`, `tool_call_result=1`) held 12/12. Roots left unpinned; range: **5 ↔ 6**.

The template now documents the pin rule these two calibrated: `EXPECT_ROOTS` only for
deterministic apps; loop-variable / session-close-variable apps record the range and rely on the
collapse guard.

## Payload-leak verification (the point of the producer fix)

- Pre-fix live DB: **4 of 130** a2a response spans carried the raw JSON-RPC result envelope as
  `output.value` (contact-extractor data-part artifacts through the co-populated MCP parse).
- Post-fix (2852 new spans, all 10 apps): **0** leaked envelopes across every new a2a response.
- Contact-extractor per-turn: 5 of 6 entry responses carry real artifact text; 1 of 6 (the
  data-part-only artifact) now has an **absent** payload — the contract-honest outcome
  ("interactions are independent of payloads"). Getting a real payload for data-part artifacts is
  an a2a-parser follow-up, not a fallback's job.
- MCP payloads unaffected: wiki-mcp / slack-tool / reservation-tool `tool_call_*` pins all held.
- Derived-side invariants after the run: every interaction still has exactly 2 legs (3374/3374),
  0 orphans/dangling parents (spot-checked via the harness assertions per trace).

## Deviations register

| item | status |
|---|---|
| Weather first-pass 2× no-tool-call turns | operator prompt artifact; resolved by tool-forcing prompt, recorded above |
| reservation / slack-tool root pins | wrong constants for oscillating apps; template rule added, ranges recorded |
| `lineage-driver2` pod Unknown | pre-existing dead driver pod, unrelated; runs used `lineage-driver3` / `mcp-lineage-driver` |
