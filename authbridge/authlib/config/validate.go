package config

import (
	"fmt"
)

// Validate checks the top-level runtime config: mode and listener combo.
// Plugin-specific validation (issuer, token URL, identity type,
// jwt_audience) lives inside each plugin's Configure and runs at
// pipeline build time.
//
// Empty pipelines are permitted: AuthBridge will run as a pass-through
// on any stage with no plugins. This supports testing scenarios and
// asymmetric deployments (e.g. inbound auth only, no outbound token
// exchange). Operators get a startup WARN per empty stage from
// WarnEmptyPipelines so the open-proxy condition is visible in logs.
func Validate(cfg *Config) error {
	switch cfg.Mode {
	case ModeEnvoySidecar, ModeWaypoint, ModeProxySidecar:
		// valid
	case "":
		return fmt.Errorf("mode is required (envoy-sidecar, waypoint, or proxy-sidecar)")
	default:
		return fmt.Errorf("unknown mode %q (valid: envoy-sidecar, waypoint, proxy-sidecar)", cfg.Mode)
	}
	return validateListeners(cfg)
}

func validateListeners(cfg *Config) error {
	switch cfg.Mode {
	case ModeEnvoySidecar:
		if cfg.Listener.ReverseProxyAddr != "" {
			return fmt.Errorf("envoy-sidecar mode does not support reverse_proxy_addr (use proxy-sidecar mode)")
		}
		if cfg.Listener.ExtAuthzAddr != "" {
			return fmt.Errorf("envoy-sidecar mode does not support ext_authz_addr (use waypoint mode)")
		}
	case ModeWaypoint:
		if cfg.Listener.ExtProcAddr != "" {
			return fmt.Errorf("waypoint mode does not support ext_proc_addr (use envoy-sidecar mode)")
		}
		if cfg.Listener.ReverseProxyAddr != "" {
			return fmt.Errorf("waypoint mode does not support reverse_proxy_addr")
		}
	case ModeProxySidecar:
		if cfg.Listener.ExtProcAddr != "" {
			return fmt.Errorf("proxy-sidecar mode does not support ext_proc_addr (use envoy-sidecar mode)")
		}
		if cfg.Listener.ExtAuthzAddr != "" {
			return fmt.Errorf("proxy-sidecar mode does not support ext_authz_addr (use waypoint mode)")
		}
		if cfg.Listener.ReverseProxyBackend == "" {
			return fmt.Errorf("proxy-sidecar mode requires listener.reverse_proxy_backend")
		}
	}
	return nil
}
