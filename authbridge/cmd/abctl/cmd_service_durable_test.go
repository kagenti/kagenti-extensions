package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// TestStopIsDurable pins the contract the PR title claims: a stop must still be a
// stop after a login. On macOS bootout alone is undone when launchd re-bootstraps
// ~/Library/LaunchAgents; on Linux `systemctl stop` leaves the unit enabled.
//
// This reads the source rather than driving the commands, because controlService
// shells out to the real supervisor and there is no seam to inject a fake. So treat
// it as a canary against the persistent forms being dropped, NOT as evidence that
// stop survives a login — that needs a real logout, which no test here performs.
func TestStopIsDurable(t *testing.T) {
	src, err := os.ReadFile("cmd_service_platform.go")
	if err != nil {
		t.Fatal(err)
	}
	body := string(src)

	t.Run("macOS pairs bootout with a persistent disable", func(t *testing.T) {
		if !strings.Contains(body, `"launchctl", "disable", target`) {
			t.Error("stop does not disable the label; it would restart at next login")
		}
	})

	t.Run("and start clears that disable, or nothing could start again", func(t *testing.T) {
		if !strings.Contains(body, `"launchctl", "enable", "gui/"+uid+"/"+launchdLabel`) {
			t.Error("loadService does not enable; a stop would make every later start fail")
		}
	})

	t.Run("systemd stop disables too, since a stopped unit stays enabled", func(t *testing.T) {
		if !strings.Contains(body, `"--user", "disable", "--now", systemdUnit`) {
			t.Error("systemd stop is transient; the unit would return at boot")
		}
		if !strings.Contains(body, `"--user", "enable", "--now", systemdUnit`) {
			t.Error("systemd start does not re-enable")
		}
	})
}

// TestUnitCarriesItsWriter covers the skew that made a live launchd job
// unmanageable on a real machine: a newer build wrote the plist, an older abctl on
// PATH could not even parse `service`.
func TestUnitCarriesItsWriter(t *testing.T) {
	p := servicePaths{
		binary:     "/u/bin/authbridge-proxy",
		configFile: "/u/.cortex/config.yaml",
		logFile:    "/u/.cortex/proxy.log",
		home:       "/u",
	}
	for _, goos := range []string{"darwin", "linux"} {
		dir := t.TempDir()
		unit := filepath.Join(dir, "unit")
		if err := os.WriteFile(unit, []byte(renderUnitFor(goos, p)), 0o600); err != nil {
			t.Fatal(err)
		}
		got := unitWriterVersion(unit)
		if got != version {
			t.Errorf("%s: unitWriterVersion = %q, want %q", goos, got, version)
		}
	}
	t.Run("an unstamped unit reads as unknown, not as a false match", func(t *testing.T) {
		unit := filepath.Join(t.TempDir(), "old")
		if err := os.WriteFile(unit, []byte("[Unit]\nDescription=old\n"), 0o600); err != nil {
			t.Fatal(err)
		}
		if got := unitWriterVersion(unit); got != "" {
			t.Errorf("got %q, want empty for an unstamped unit", got)
		}
	})
}

// TestRotateLog bounds the crash-loop case: KeepAlive plus ThrottleInterval 10 is
// ~6 restarts a minute, each writing a startup banner into one unrotated file.
func TestRotateLog(t *testing.T) {
	t.Run("rotates once past the cap", func(t *testing.T) {
		dir := t.TempDir()
		log := filepath.Join(dir, "proxy.log")
		if err := os.WriteFile(log, make([]byte, 100), 0o600); err != nil {
			t.Fatal(err)
		}
		rotateLog(log, 50)
		if _, err := os.Stat(log + ".1"); err != nil {
			t.Errorf("previous generation not kept: %v", err)
		}
		if _, err := os.Stat(log); !os.IsNotExist(err) {
			t.Error("the live path should be free for the next spawn to create")
		}
	})

	t.Run("leaves a small log alone", func(t *testing.T) {
		dir := t.TempDir()
		log := filepath.Join(dir, "proxy.log")
		if err := os.WriteFile(log, make([]byte, 10), 0o600); err != nil {
			t.Fatal(err)
		}
		rotateLog(log, 50)
		if _, err := os.Stat(log); err != nil {
			t.Errorf("rotated a log under the cap: %v", err)
		}
		if _, err := os.Stat(log + ".1"); err == nil {
			t.Error("created a generation it did not need")
		}
	})

	t.Run("a missing log is not an error", func(t *testing.T) {
		rotateLog(filepath.Join(t.TempDir(), "absent.log"), 1)
	})
}

// TestEstablishedConns_CannotTellIsNotZero: reporting "0 sessions" when we simply
// cannot look would be worse than staying quiet.
func TestEstablishedConns_CannotTellIsNotZero(t *testing.T) {
	if got := establishedConns(""); got != -1 {
		t.Errorf("establishedConns(\"\") = %d, want -1 (unknown)", got)
	}
	if got := establishedConns("not-an-address"); got != -1 {
		t.Errorf("malformed address = %d, want -1 (unknown)", got)
	}
}
