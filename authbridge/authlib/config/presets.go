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
		setDefault(&cfg.Listener.ReverseProxyAddr, ":8080")
		setDefault(&cfg.Listener.ForwardProxyAddr, ":8081")
		// Outbound transparent listener for enforce-redirect mode. Binding it
		// is harmless when nothing is redirected here (cooperative HTTP_PROXY
		// deployments simply never receive connections on it).
		setDefault(&cfg.Listener.TransparentProxyAddr, ":8082")
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
