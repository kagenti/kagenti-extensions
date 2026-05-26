package lineage

import (
	"encoding/json"
	"fmt"
	"strings"
)

// Config holds the per-plugin configuration decoded from the pipeline YAML.
type Config struct {
	// OTelEndpoint is the OTLP gRPC endpoint (host:port or http://host:port).
	// Default: "localhost:4317"
	OTelEndpoint string `json:"otel_endpoint"`

	// EmitBodyHash when true adds a SHA-256 hash of the request body as a
	// span attribute. Off by default to avoid accidental PII exposure.
	EmitBodyHash bool `json:"emit_body_hash"`

	// BypassPaths lists URL path prefixes that should not generate lineage
	// hops. Useful for suppressing infrastructure polling (agent-card
	// discovery, health checks) that would otherwise flood the lineage graph.
	// Default: ["/.well-known/", "/healthz", "/readyz", "/health"]
	BypassPaths []string `json:"bypass_paths"`
}

func defaultConfig() Config {
	return Config{
		OTelEndpoint: "localhost:4317",
		BypassPaths:  []string{"/.well-known/", "/healthz", "/readyz", "/health"},
	}
}

func decodeConfig(raw json.RawMessage) (Config, error) {
	cfg := defaultConfig()
	if len(raw) == 0 {
		return cfg, nil
	}
	if err := json.Unmarshal(raw, &cfg); err != nil {
		return Config{}, fmt.Errorf("lineage-telemetry config: %w", err)
	}
	if cfg.OTelEndpoint == "" {
		cfg.OTelEndpoint = "localhost:4317"
	}
	// Strip http:// or https:// prefix — gRPC NewClient expects host:port only.
	cfg.OTelEndpoint = strings.TrimPrefix(cfg.OTelEndpoint, "https://")
	cfg.OTelEndpoint = strings.TrimPrefix(cfg.OTelEndpoint, "http://")
	return cfg, nil
}
