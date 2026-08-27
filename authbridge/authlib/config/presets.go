package config

// ApplyPreset fills in mode-specific defaults for listener addresses.
// Plugin-specific defaults live inside each plugin's Configure (see
// authbridge/docs/plugin-reference.md); the runtime config no
// longer shapes plugin behavior.
func ApplyPreset(cfg *Config) {
	switch cfg.Mode {
	case ModeEnvoySidecar:
		setDefault(&cfg.Listener.ExtProcAddr, ":9090")

	case ModeWaypoint:
		setDefault(&cfg.Listener.ExtAuthzAddr, ":9090")
		setDefault(&cfg.Listener.ForwardProxyAddr, ":8080")

	case ModeProxySidecar:
		// Fill an addr default only for an active role (empty Roles => both),
		// so a forward-only or reverse-only deployment doesn't bind the proxy
		// it didn't ask for. main.go starts a proxy iff its role is active.
		roles := cfg.Listener.ActiveRoles()
		if roles[RoleReverse] {
			// The two inbound mechanisms are mutually exclusive: transparent
			// interception REDIRECTs to its own port and leaves the agent on the
			// port it already binds, so filling reverse_proxy_addr there would
			// bind a port nothing routes to (and, if it collided with the agent's
			// own port, would break the pod).
			if cfg.Listener.InboundTransparent() {
				setDefault(&cfg.Listener.TransparentInboundAddr, ":8083")
			} else {
				setDefault(&cfg.Listener.ReverseProxyAddr, ":8080")
			}
		}
		if roles[RoleForward] {
			setDefault(&cfg.Listener.ForwardProxyAddr, ":8081")
			// Outbound transparent listener for enforce-redirect mode. Binding it
			// is harmless when nothing is redirected here (cooperative HTTP_PROXY
			// deployments simply never receive connections on it).
			setDefault(&cfg.Listener.TransparentProxyAddr, ":8082")
		}
	}

	// Session events API is default-on for every mode. Operators who
	// want to turn it off can disable session tracking entirely via
	// session.enabled: false — main.go skips the API server when the
	// store itself is nil.
	setDefault(&cfg.Listener.SessionAPIAddr, ":9094")
}

func setDefault(field *string, value string) {
	if *field == "" {
		*field = value
	}
}
