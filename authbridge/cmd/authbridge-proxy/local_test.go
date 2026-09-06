package main

import (
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"github.com/rossoctl/cortex/authbridge/authlib/config"
)

// writeBuiltinConfig must produce a config file in cortexDir that loads, presets,
// and validates cleanly and describes a forward-only TLS-bridge observe
// pipeline pointed at caDir — otherwise --local would fail at boot instead of
// giving users a working, hot-reloadable local setup.
func TestDemoConfig_WriteLoadsAndValidates(t *testing.T) {
	cortexDir := t.TempDir()
	caDir := filepath.Join(cortexDir, "ca")

	p, err := writeBuiltinConfig(cortexDir, caDir)
	if err != nil {
		t.Fatalf("writeBuiltinConfig: %v", err)
	}
	if filepath.Dir(p) != cortexDir {
		t.Errorf("config written to %q, want inside %q", p, cortexDir)
	}

	cfg, err := config.Load(p)
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	config.ApplyPreset(cfg)
	if err := config.Validate(cfg); err != nil {
		t.Fatalf("Validate: %v", err)
	}

	if cfg.Mode != config.ModeProxySidecar {
		t.Errorf("Mode = %q, want %q", cfg.Mode, config.ModeProxySidecar)
	}

	roles := cfg.Listener.ActiveRoles()
	if !roles[config.RoleForward] || roles[config.RoleReverse] {
		t.Errorf("expected forward-only roles, got %v", roles)
	}

	// The listeners the demo uses must bind loopback on the uncommon ports the
	// installer probes/prints, never a wildcard that would expose an open forward
	// proxy, the stats endpoint, or the unauthenticated session API (decrypted
	// bodies + injected tokens) to the LAN. The transparent listener isn't started
	// under --local (main.go gates it), so it's not asserted here.
	if got := cfg.Listener.ForwardProxyAddr; got != "127.0.0.1:47600" {
		t.Errorf("ForwardProxyAddr = %q, want loopback 127.0.0.1:47600", got)
	}
	if got := cfg.Listener.SessionAPIAddr; got != "127.0.0.1:47601" {
		t.Errorf("SessionAPIAddr = %q, want loopback 127.0.0.1:47601", got)
	}
	if got := cfg.Stats.StatsAddress; got != "127.0.0.1:47602" {
		t.Errorf("Stats.StatsAddress = %q, want loopback 127.0.0.1:47602", got)
	}

	if cfg.TLSBridge == nil {
		t.Fatalf("expected tls_bridge config, got nil")
	}
	if cfg.TLSBridge.Mode != "enabled" || !cfg.TLSBridge.GenerateCA {
		t.Errorf("expected tls_bridge enabled with generate_ca, got %+v", cfg.TLSBridge)
	}
	if cfg.TLSBridge.CADir != caDir {
		t.Errorf("CADir = %q, want %q", cfg.TLSBridge.CADir, caDir)
	}

	// Assert the exact parser set and order, not just the count — a swapped or
	// renamed plugin would otherwise pass silently.
	gotPlugins := make([]string, len(cfg.Pipeline.Outbound.Plugins))
	for i, p := range cfg.Pipeline.Outbound.Plugins {
		gotPlugins[i] = p.Name
	}
	// tool-prune must come last: it is the request-body mutator, and the
	// pipeline refuses to build a chain where a body reader follows it.
	wantPlugins := []string{"inference-parser", "mcp-parser", "a2a-parser", "tool-prune"}
	if !slices.Equal(gotPlugins, wantPlugins) {
		t.Errorf("outbound plugins = %v, want %v", gotPlugins, wantPlugins)
	}

	// tool-prune ships inert, and that is a property worth pinning: the demo
	// must never silently start rewriting a user's traffic. The empty remove
	// list is the guard — with no tool named there is nothing to remove, whatever
	// the policy — so filling the list is the single, deliberate act that
	// enables it. Asserting the policy too would just pin a default that is
	// meant to be edited.
	var tp *config.PluginEntry
	for i := range cfg.Pipeline.Outbound.Plugins {
		if cfg.Pipeline.Outbound.Plugins[i].Name == "tool-prune" {
			tp = &cfg.Pipeline.Outbound.Plugins[i]
		}
	}
	if tp == nil {
		t.Fatal("tool-prune entry not found")
	}
	if !strings.Contains(string(tp.Config), "\"remove\":[]") &&
		!strings.Contains(string(tp.Config), "\"remove\": []") {
		t.Errorf("tool-prune must ship with an empty remove list, got %s", tp.Config)
	}
}

// TestWriteDemoConfig_PreservesAnExistingFile: the config's own header invites
// editing it, and `abctl tools scan --write` writes a prune list into it. This
// function also runs before any port is bound, so an unconditional overwrite
// meant a --local start that then failed on a port clash silently destroyed those
// edits — which is exactly how a populated remove list was lost in practice.
func TestWriteDemoConfig_PreservesAnExistingFile(t *testing.T) {
	cortexDir := t.TempDir()
	caDir := filepath.Join(cortexDir, "ca")
	p, err := writeBuiltinConfig(cortexDir, caDir)
	if err != nil {
		t.Fatal(err)
	}
	edited := "# operator edit\nmode: proxy-sidecar\n"
	if err := os.WriteFile(p, []byte(edited), 0o644); err != nil {
		t.Fatal(err)
	}
	// A second call — a restart — must not clobber it.
	p2, err := writeBuiltinConfig(cortexDir, caDir)
	if err != nil {
		t.Fatal(err)
	}
	if p2 != p {
		t.Errorf("path changed: %q vs %q", p2, p)
	}
	got, err := os.ReadFile(p)
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != edited {
		t.Errorf("edits were overwritten:\n%s", got)
	}
}
