package probe.outbound
import future.keywords

# Outbound decision probe for the discovery-driven UC-1 `github_agent` scenario. Adapted from
# 5.3's `probe.rego` for two UC-1-specific facts:
#
#   1. USER GATE ONLY. Under real UC-1 the single generic `github-agent.agent` role maps to no
#      specific tool scope, so the generated `target_ok` (agent->tool) is degenerate/empty and the
#      real `allow` (subject_ok AND target_ok) would deny everything. This probe therefore binds
#      against the `subject_ok` maps ALONE — the user-gating slice phase-1 validates. `target_ok`
#      is documented as degenerate, not probed.
#
#   2. EXACT-NAME MATCH. `scenario_uc1.py` stores the FULL discovered scope names
#      (`github-tool.source-read`, ...) — the same strings the generated data maps contain — so
#      `input.function_name` is matched to a subject scope by plain string equality. No 5.3-style
#      prefix-stripping token-set soft match (that was 5.3's device for bare names; here both sides
#      are already prefixed).
gen := data.authz.github_agent.outbound

# Tool scopes the user (subject) is entitled to, via the generated user->tool data maps only.
subject_scopes contains scope if {
    some role in gen.subject_roles[input.subject]
    some scope in gen.outbound_subject_role_scopes[role]
}

default allow := false
allow if {
    input.function_name in subject_scopes
}
