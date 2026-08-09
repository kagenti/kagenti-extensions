package probe.outbound_eval

import future.keywords

# Generalized outbound decision probe for the multi-entity eval scenarios (scenario_eval_*.py).
# Unlike probe.rego (which hardcodes the fixed github_agent scenario), this probe is parameterized
# by ``input.agent_id`` so one file serves every agent in every heavy scenario. Same token
# soft-match logic as probe.rego: the generated ``{agent}.outbound.rego`` exposes only data + an
# `allow` keyed on a concrete tool/target scope; this probe binds an inbound `input.function_name`
# to that scope by soft-matching their token sets, so the test can drive the outbound gate the way
# a caller would (by function name) rather than by pre-resolved scope.

gen := data.authz[input.agent_id].outbound

# Case/separator-insensitive token set: "Source.Read" and "source-read" both -> {"source","read"}.
tokens(s) := {lower(t) | some t in regex.split(`[._-]+`, s)}

# Target scopes the user (subject) is entitled to on the target.
subject_scopes contains scope if {
    some role in gen.subject_roles[input.subject]
    some scope in gen.subject_role_scopes[role]
    scope in gen.target_scopes[input.target]
}

# Target scopes the agent itself is entitled to reach on the target.
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
