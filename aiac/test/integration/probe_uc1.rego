package probe.outbound
import future.keywords

# Outbound decision probe for the discovery-driven UC-1 `github_agent` scenario. Adapted from
# 5.3's `probe.rego` for two UC-1-specific facts:
#
#   1. PER-SCOPE AND (both gates). Outbound access is a per-scope two-gate AND: a requested tool
#      scope is allowed iff BOTH the user (subject) is granted it AND the agent's own operator roles
#      reach it. This probe binds both gates against the generated data maps. UC-1 has a single tool,
#      so the capability gate uses the agent's own capability map (`agent_role_scopes`) directly —
#      equivalent to the writer's `target_scopes[input.target]` for that one target — and the probe
#      input stays `{subject, function_name}` (no `target` key needed).
#
#   2. EXACT-NAME MATCH. `scenario_uc1.py` stores the FULL discovered scope names
#      (`github-tool.source-read`, ...) — the same strings the generated data maps contain — so
#      `input.function_name` is matched to a subject/agent scope by plain string equality. No 5.3-style
#      prefix-stripping token-set soft match (that was 5.3's device for bare names; here both sides
#      are already prefixed).
gen := data.authz.team1_github_agent.outbound

# Tool scopes the user (subject) is entitled to, via the generated user->tool data maps (subject gate).
subject_scopes contains scope if {
    some role in gen.subject_roles[input.subject]
    some scope in gen.subject_role_scopes[role]
}

# Tool scopes the agent's own operator roles reach (capability gate; single-target UC-1). Iterate
# the map's VALUES (each a list of scope names) and flatten — ``some scopes in obj`` binds each value.
agent_scopes contains scope if {
    some scopes in gen.agent_role_scopes
    some scope in scopes
}

default allow := false
allow if {
    input.function_name in subject_scopes
    input.function_name in agent_scopes
}
