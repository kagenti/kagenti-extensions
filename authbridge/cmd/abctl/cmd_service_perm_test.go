package main

import (
	"bytes"
	"os"
	"path/filepath"
	"testing"
)

// TestTightenLog covers what install relies on: the log records every host the
// proxy talks to, and the supervisor would create it 0644.
func TestTightenLog(t *testing.T) {
	t.Run("tightens one an earlier install left loose", func(t *testing.T) {
		log := filepath.Join(t.TempDir(), "proxy.log")
		if err := os.WriteFile(log, []byte("old\n"), 0o644); err != nil {
			t.Fatal(err)
		}
		var errOut bytes.Buffer
		tightenLog(log, &errOut)

		fi, err := os.Stat(log)
		if err != nil {
			t.Fatal(err)
		}
		if got := fi.Mode().Perm(); got != 0o600 {
			t.Errorf("mode = %o, want 600", got)
		}
		// Append-only: losing a log to a permission fix would be its own bug.
		b, err := os.ReadFile(log)
		if err != nil {
			t.Fatal(err)
		}
		if string(b) != "old\n" {
			t.Errorf("content = %q, want the previous content kept", string(b))
		}
		if errOut.Len() != 0 {
			t.Errorf("unexpected complaint: %s", errOut.String())
		}
	})

	t.Run("creates it 0600 when absent, so the supervisor never makes it 0644", func(t *testing.T) {
		log := filepath.Join(t.TempDir(), "proxy.log")
		var errOut bytes.Buffer
		tightenLog(log, &errOut)

		fi, err := os.Stat(log)
		if err != nil {
			t.Fatalf("not created: %v", err)
		}
		if got := fi.Mode().Perm(); got != 0o600 {
			t.Errorf("mode = %o, want 600", got)
		}
	})

	t.Run("an unwritable path is reported, not fatal", func(t *testing.T) {
		// A directory that cannot be created into: install must still proceed.
		log := filepath.Join(t.TempDir(), "nope", "proxy.log")
		var errOut bytes.Buffer
		tightenLog(log, &errOut) // must not panic
		if _, err := os.Stat(log); err == nil {
			t.Error("unexpectedly created the file")
		}
	})
}
