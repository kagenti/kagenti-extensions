package probe.outbound_eval

import rego.v1

gen := data.authbridge.client.outbound.request

tokens(s) := {lower(t) | some t in regex.split(`[._-]+`, s)}

subject_ok if {
	some role in gen.subject_roles[input.subject]
	some scope in gen.subject_role_allow_scopes[role]
	tokens(scope) == tokens(input.function_name)
}

target_ok if {
	some scope in gen.target_allow_scopes[input.target]
	tokens(scope) == tokens(input.function_name)
}

default allow := false

allow if {
	subject_ok
	target_ok
}
