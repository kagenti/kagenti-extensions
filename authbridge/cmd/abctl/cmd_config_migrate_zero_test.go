package main

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// An explicit zero value is PRESENT but parses as the zero value, so an
// absence check based on the parsed value appends a duplicate key.
func TestMigrate_ExplicitZeroValues(t *testing.T) {
	for _, tc := range []struct{ name, line string }{
		{"bind_loopback_only false", "  bind_loopback_only: false"},
		{"empty health_addr", `  health_addr: ""`},
		{"empty transparent", `  transparent_proxy_addr: ""`},
	} {
		t.Run(tc.name, func(t *testing.T) {
			dir := t.TempDir()
			p := filepath.Join(dir, "config.yaml")
			body := "mode: proxy-sidecar\nlistener:\n  roles: [forward]\n  forward_proxy_addr: 127.0.0.1:47600\n" +
				tc.line + "\nstats:\n  address: 127.0.0.1:47602\n"
			if err := os.WriteFile(p, []byte(body), 0o600); err != nil {
				t.Fatal(err)
			}
			var out bytes.Buffer
			_, err := migrateConfig(p, &out)
			after, _ := os.ReadFile(p)
			key := strings.TrimSpace(strings.SplitN(tc.line, ":", 2)[0])
			n := strings.Count(string(after), key+":")
			t.Logf("err=%v  occurrences of %s = %d", err, key, n)
			if n > 1 {
				t.Errorf("DUPLICATE %s written (%d times)", key, n)
			}
			if err != nil {
				t.Errorf("migration errored on a valid config: %v", err)
			}
		})
	}
}

// TestMigrate_FlowStyleListener: a flow-style block cannot take block-style
// children. It already failed closed; now it says why.
func TestMigrate_FlowStyleListener(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "config.yaml")
	body := "mode: proxy-sidecar\nlistener: {roles: [forward], forward_proxy_addr: 127.0.0.1:47600}\n" +
		"stats:\n  address: 127.0.0.1:47602\n"
	if err := os.WriteFile(p, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	var out bytes.Buffer
	_, err := migrateConfig(p, &out)
	if err == nil {
		t.Fatal("expected a refusal for a flow-style listener block")
	}
	if !strings.Contains(err.Error(), "flow style") {
		t.Errorf("error does not explain the shape: %v", err)
	}
	after, _ := os.ReadFile(p)
	if string(after) != body {
		t.Error("the original config was modified")
	}
}

// TestWildcardListeners names what is exposed, which is what a refusal reports.
func TestWildcardListeners(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "config.yaml")
	body := "mode: proxy-sidecar\nlistener:\n  roles: [forward]\n" +
		"  forward_proxy_addr: 127.0.0.1:47600\n  health_addr: \":9091\"\n"
	if err := os.WriteFile(p, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	got := wildcardListeners(p)
	found := false
	for _, g := range got {
		if g == "health_addr" {
			found = true
		}
		if g == "forward_proxy_addr" {
			t.Error("a loopback-bound listener was reported as exposed")
		}
	}
	if !found {
		t.Errorf("health_addr on :9091 not reported as exposed; got %v", got)
	}
}
