package main

import (
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"testing"
	"time"
)

// TestWaitBootedOut_RealLaunchd drives real launchctl against a throwaway label,
// reproducing the failure a real upgrade hit: `launchctl bootout` returns while
// teardown is still in progress, and bootstrapping into that window fails with
// "Bootstrap failed: 5: Input/output error". Our own teardown is slow — the supervisor
// forwards SIGTERM and waits out the proxy's graceful shutdown — so the window is wide
// enough to lose. A trivial job dies fast enough to hide it, which is why every test
// starting from nothing, or running uninstall first, passed.
func TestWaitBootedOut_RealLaunchd(t *testing.T) {
	if runtime.GOOS != "darwin" {
		t.Skip("launchd only")
	}
	if _, err := exec.LookPath("launchctl"); err != nil {
		t.Skip("no launchctl")
	}
	uid := strconv.Itoa(os.Getuid())
	label := "io.rossoctl.cortex.test.bootout"
	target := "gui/" + uid + "/" + label
	dir := t.TempDir()

	// A job that is deliberately slow to die, like the supervisor.
	script := filepath.Join(dir, "slow.sh")
	if err := os.WriteFile(script,
		[]byte("#!/bin/sh\ntrap 'sleep 4; exit 0' TERM\nwhile :; do sleep 1; done\n"), 0o700); err != nil { //nolint:gosec
		t.Fatal(err)
	}
	plist := filepath.Join(dir, label+".plist")
	body := `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>` + label + `</string>
  <key>ProgramArguments</key><array><string>` + script + `</string></array>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
</dict></plist>`
	if err := os.WriteFile(plist, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		_ = exec.Command("launchctl", "bootout", target).Run() //nolint:errcheck
		_ = exec.Command("pkill", "-f", script).Run()          //nolint:errcheck
	})

	_ = exec.Command("launchctl", "bootout", target).Run() //nolint:errcheck
	waitBootedOut(target, 10*time.Second)
	if out, err := exec.Command("launchctl", "bootstrap", "gui/"+uid, plist).CombinedOutput(); err != nil {
		t.Skipf("cannot bootstrap a test agent here: %v: %s", err, out)
	}
	_ = exec.Command("launchctl", "kickstart", "-p", target).Run() //nolint:errcheck

	// The race only exists while a job is actually RUNNING and slow to die. Without
	// this guard the test passed in 0.07s against a job launchd had never started —
	// green, and proving nothing. launchd refuses to start agents added mid-session in
	// some domains (see supervise.go), so skip loudly rather than pass vacuously.
	running := false
	for i := 0; i < 20; i++ {
		out, _ := exec.Command("launchctl", "print", target).CombinedOutput()
		for _, line := range strings.Split(string(out), "\n") {
			if strings.Contains(line, "state = running") {
				running = true
			}
		}
		if running {
			break
		}
		time.Sleep(200 * time.Millisecond)
	}
	if !running {
		t.Skip("launchd would not start the test agent in this domain; cannot exercise the race")
	}

	// Tear it down and confirm waitBootedOut does not return until the label is gone.
	_ = exec.Command("launchctl", "bootout", target).Run() //nolint:errcheck
	if !waitBootedOut(target, 20*time.Second) {
		t.Fatal("waitBootedOut timed out; teardown never completed")
	}
	// Deliberately NOT asserting how long it waited. That assertion was here and was
	// flaky: launchd sometimes tears the job down in milliseconds, so "must take at
	// least a second" failed on a correct implementation. The contract that matters is
	// the post-condition below — the label is gone, and the bootstrap that used to hit
	// EIO now succeeds.
	// The label must really be absent now, which is what makes the next bootstrap safe.
	if err := exec.Command("launchctl", "print", target).Run(); err == nil {
		t.Error("waitBootedOut returned true while the label is still in the domain")
	}
	// And the bootstrap that previously failed with EIO must now succeed.
	out, err := exec.Command("launchctl", "bootstrap", "gui/"+uid, plist).CombinedOutput()
	if err != nil {
		t.Errorf("bootstrap after waitBootedOut still failed: %v: %s", err, out)
	}
	if strings.Contains(string(out), "Input/output error") {
		t.Errorf("still hitting the EIO race: %s", out)
	}
}
