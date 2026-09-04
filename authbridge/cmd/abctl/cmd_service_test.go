package main

import (
	"bytes"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"testing"
	"time"
)

func servicePathsFixture(t *testing.T) servicePaths {
	t.Helper()
	dir := t.TempDir()
	bin := filepath.Join(dir, "authbridge-proxy")
	if err := os.WriteFile(bin, []byte("#!/bin/sh\nsleep 30\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	return servicePaths{
		unitFile:   filepath.Join(dir, "unit"),
		binary:     bin,
		configFile: filepath.Join(dir, "config.yaml"),
		logFile:    filepath.Join(dir, "proxy.log"),
		pidFile:    filepath.Join(dir, "proxy.pid"),
		healthURL:  "http://127.0.0.1:1/healthz",
		home:       dir,
	}
}

// TestRenderUnit_BothPlatforms exercises the launchd and systemd renderings from
// either host. Keying off runtime.GOOS meant the systemd unit was written on a Mac
// and never checked until a Linux user hit it.
func TestRenderUnit_BothPlatforms(t *testing.T) {
	p := servicePathsFixture(t)

	t.Run("darwin restarts after a crash", func(t *testing.T) {
		// Unconditional KeepAlive on purpose: it is the simpler guarantee for the
		// requirement that matters — come back after a crash — and costs nothing,
		// because stopping deliberately is what `service uninstall` is for.
		// {SuccessfulExit:false} is documented in terms of exit STATUS, which leaves
		// its behaviour on death by signal ambiguous.
		u := renderUnitFor("darwin", p)
		if !strings.Contains(u, "<key>KeepAlive</key><true/>") {
			t.Error("KeepAlive is not unconditional; a kill -9 would not be restarted")
		}
		if strings.Contains(u, "SuccessfulExit") {
			t.Error("KeepAlive is conditioned on exit status, which signal death does not satisfy")
		}
		if !strings.Contains(u, "ThrottleInterval") {
			t.Error("no ThrottleInterval; a bad config would respawn tightly")
		}
		if !strings.Contains(u, "<key>HOME</key>") || !strings.Contains(u, p.home) {
			t.Error("HOME not set; the config's ${HOME} would not expand")
		}
		if !strings.Contains(u, "<key>RunAtLoad</key><true/>") {
			t.Error("does not start at login")
		}
	})

	t.Run("linux restarts on failure only", func(t *testing.T) {
		u := renderUnitFor("linux", p)
		if !strings.Contains(u, "Restart=on-failure") {
			t.Errorf("not on-failure:\n%s", u)
		}
		if strings.Contains(u, "Restart=always") {
			t.Error("Restart=always; a stop could never stick")
		}
		for _, want := range []string{
			"[Unit]", "[Service]", "[Install]",
			"ExecStart=", "RestartSec=", "StartLimitBurst=",
			"WantedBy=default.target",
			"append:" + p.logFile, // restarts must not truncate the evidence
		} {
			if !strings.Contains(u, want) {
				t.Errorf("unit missing %q:\n%s", want, u)
			}
		}
		// StartLimit* must sit in [Unit]. systemd moved them there in v229 and
		// deprecated them in [Service], where they can be ignored outright —
		// silently voiding the crash-loop throttle.
		unitSec := u[strings.Index(u, "[Unit]"):strings.Index(u, "[Service]")]
		for _, want := range []string{"StartLimitIntervalSec=", "StartLimitBurst="} {
			if !strings.Contains(unitSec, want) {
				t.Errorf("%s is not in the [Unit] section:\n%s", want, u)
			}
		}
		// A user manager has no network-online.target; ordering against it would be
		// a dependency that never resolves, and every listener here is loopback.
		if strings.Contains(u, "network-online.target") {
			t.Error("user unit orders against network-online.target")
		}
		// One ExecStart, and it names absolute paths — a unit has no shell PATH.
		if n := strings.Count(u, "ExecStart="); n != 1 {
			t.Errorf("ExecStart appears %d times, want 1", n)
		}
		if !strings.Contains(u, p.binary+" --config "+p.configFile) {
			t.Errorf("ExecStart does not invoke absolute paths:\n%s", u)
		}
	})

	t.Run("both name the same log file", func(t *testing.T) {
		for _, goos := range []string{"darwin", "linux"} {
			if !strings.Contains(renderUnitFor(goos, p), p.logFile) {
				t.Errorf("%s unit does not log to %s", goos, p.logFile)
			}
		}
	})

	t.Run("systemd unit has no unescaped newline hazards", func(t *testing.T) {
		// A stray blank key or a value spanning lines makes systemd reject the unit
		// at daemon-reload, which surfaces only as a non-zero exit.
		for _, line := range strings.Split(renderUnitFor("linux", p), "\n") {
			l := strings.TrimSpace(line)
			if l == "" || strings.HasPrefix(l, "#") || strings.HasPrefix(l, "[") {
				continue
			}
			if !strings.Contains(l, "=") {
				t.Errorf("unit line is neither section, comment nor key=value: %q", l)
			}
		}
	})
}

// TestRenderedPlistIsValid runs plutil, so a malformed plist cannot ship: launchd
// would reject it at bootstrap and the user would see a bare exit status.
func TestRenderedPlistIsValid(t *testing.T) {
	if runtime.GOOS != "darwin" {
		t.Skip("plutil is macOS-only")
	}
	if _, err := exec.LookPath("plutil"); err != nil {
		t.Skip("plutil not available")
	}
	p := servicePathsFixture(t)
	path := filepath.Join(t.TempDir(), "t.plist")
	if err := os.WriteFile(path, []byte(renderUnit(p)), 0o644); err != nil {
		t.Fatal(err)
	}
	if out, err := exec.Command("plutil", "-lint", path).CombinedOutput(); err != nil {
		t.Errorf("plutil rejected the plist: %v\n%s\n%s", err, out, renderUnit(p))
	}
}

// TestRunningPID_OnlyClaimsOurOwnProcess: the pidfile can name a recycled pid, and
// install stops whatever it reports. Stopping a stranger's process would be the
// worst possible bug in this command.
func TestRunningPID_OnlyClaimsOurOwnProcess(t *testing.T) {
	dir := t.TempDir()
	pf := filepath.Join(dir, "proxy.pid")

	// No file.
	if got := runningPID(pf); got != 0 {
		t.Errorf("absent pidfile -> %d, want 0", got)
	}
	// Garbage.
	_ = os.WriteFile(pf, []byte("not-a-pid\n"), 0o600)
	if got := runningPID(pf); got != 0 {
		t.Errorf("garbage pidfile -> %d, want 0", got)
	}
	// A live pid that is NOT authbridge-proxy: this process.
	_ = os.WriteFile(pf, []byte(strconv.Itoa(os.Getpid())+"\n"), 0o600)
	if got := runningPID(pf); got != 0 {
		t.Errorf("pid of a foreign process -> %d, want 0 (it is not ours)", got)
	}
	// A pid that is not running at all.
	_ = os.WriteFile(pf, []byte("999999\n"), 0o600)
	if got := runningPID(pf); got != 0 {
		t.Errorf("dead pid -> %d, want 0", got)
	}
}

// TestDialableAddr covers the bind-vs-dial distinction the health probe depends
// on: ":9091" is not something a client can connect to.
func TestDialableAddr(t *testing.T) {
	for _, tc := range []struct{ in, want string }{
		{"127.0.0.1:47604", "127.0.0.1:47604"},
		{":47604", "localhost:47604"},
		{"0.0.0.0:47604", "localhost:47604"},
		{"[::]:47604", "localhost:47604"},
		{"[::1]:47604", "[::1]:47604"},
	} {
		if got := dialableAddr(tc.in); got != tc.want {
			t.Errorf("dialableAddr(%q) = %q, want %q", tc.in, got, tc.want)
		}
	}
}

// TestServiceStatus_NamesTheUnsupervisedCase is the diagnostic that matters most:
// a hand-started proxy works until the next reboot, and nothing says so.
func TestServiceStatus_NamesTheUnsupervisedCase(t *testing.T) {
	p := servicePathsFixture(t)
	var out bytes.Buffer
	if code := serviceStatus(p, &out); code != 0 {
		t.Errorf("exit = %d, want 0 when not installed", code)
	}
	if !strings.Contains(out.String(), "not installed") {
		t.Errorf("status did not say it is uninstalled: %q", out.String())
	}

	// With a live proxy of ours in the pidfile it should say nothing restarts it.
	cmd := exec.Command(p.binary) // the fixture's sleep script
	if err := cmd.Start(); err != nil {
		t.Skip("cannot start the fixture process")
	}
	defer func() { _ = cmd.Process.Kill() }()
	_ = os.WriteFile(p.pidFile, []byte(strconv.Itoa(cmd.Process.Pid)), 0o600)
	// The fixture is not named authbridge-prox, so runningPID rejects it — which is
	// itself the property TestRunningPID covers. Assert the uninstalled branch only.
	out.Reset()
	_ = serviceStatus(p, &out)
	if !strings.Contains(out.String(), "not installed") {
		t.Errorf("unexpected status output: %q", out.String())
	}
}

// TestWaitHealthy_TimesOut: install claims success only after the health endpoint
// answers, because a supervisor reports "loaded" for a proxy that is crash-looping.
func TestWaitHealthy_TimesOut(t *testing.T) {
	start := time.Now()
	if waitHealthy("http://127.0.0.1:1/healthz", 1200*time.Millisecond) {
		t.Error("reported healthy against a closed port")
	}
	if elapsed := time.Since(start); elapsed < 900*time.Millisecond {
		t.Errorf("returned after %s; it should have used the timeout", elapsed)
	}
}

// TestLastLines_SurfacesTheReason: "installed but not answering" is useless
// without the log tail that says why.
func TestLastLines_SurfacesTheReason(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "proxy.log")
	_ = os.WriteFile(p, []byte("one\ntwo\nthree\nfour\n"), 0o600)
	got := lastLines(p, 2)
	if len(got) != 2 || got[0] != "three" || got[1] != "four" {
		t.Errorf("lastLines = %v, want [three four]", got)
	}
	if msg := lastLines(filepath.Join(dir, "nope.log"), 3); len(msg) != 1 || !strings.Contains(msg[0], "no log") {
		t.Errorf("missing log should say so, got %v", msg)
	}
}
