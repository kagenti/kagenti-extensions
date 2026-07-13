package probe.outbound
import future.keywords

# Outbound decision probe for the fixed `github_agent` scenario. The generated
# `github_agent.outbound.rego` exposes only data + an `allow` keyed on a concrete
# tool scope; this probe binds an inbound `input.function_name` to a tool scope by
# soft-matching their token sets, so the test can drive the outbound gate the way a
# caller would (by function name) rather than by pre-resolved scope.
gen := data.authz.github_agent.outbound

# Case/separator-insensitive token set: "Source.Read" and "source-read" both -> {"source","read"}.
tokens(s) := {lower(t) | some t in regex.split(`[._-]+`, s)}

# Tool scopes the user (subject) is entitled to on the target.
subject_scopes contains scope if {
    some role in gen.subject_roles[input.subject]
    some scope in gen.outbound_subject_role_scopes[role]
    scope in gen.target_scopes[input.target]
}

# Tool scopes the agent is entitled to on the target.
agent_allowed contains scope if {
    some role in gen.agent_roles
    some scope in gen.agent_role_scopes[role]
    scope in gen.target_scopes[input.target]
}

default allow := false
allow if {
    some s in subject_scopes
    tokens(s) == tokens(input.function_name)
    some a in agent_allowed
    tokens(a) == tokens(input.function_name)
}
