package main

import (
	"bufio"
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"runtime"
	"strconv"
	"strings"
	"syscall"
	"time"
)

func newFlagSet(name string, stderr io.Writer) *flag.FlagSet {
	fs := flag.NewFlagSet(name, flag.ContinueOnError)
	fs.SetOutput(stderr)
	return fs
}

func supervisorName() string {
	if runtime.GOOS == "darwin" {
		return "launchd user agent"
	}
	return "systemd user unit"
}

// renderUnit produces the plist or unit file.
//
// Two choices are load-bearing on both platforms:
//
//   - Restart on FAILURE only. launchd's KeepAlive=true respawns even after a
//     clean exit, which makes a deliberate stop impossible; SuccessfulExit=false
//     restarts a crash and honours a stop.
//   - A throttle. A config error is fatal at startup, so without one a bad edit
//     becomes a tight respawn loop.
func renderUnit(p servicePaths) string { return renderUnitFor(runtime.GOOS, p) }

// renderUnitFor takes the OS explicitly so both renderings are testable from
// either platform. Without it the systemd unit would be written on a Mac and
// never exercised until a Linux user hit it.
func renderUnitFor(goos string, p servicePaths) string {
	if goos == "darwin" {
		// HOME is set explicitly: the config interpolates ${HOME} at load, and an
		// agent's environment is minimal enough not to rely on inheritance.
		return `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>` + launchdLabel + `</string>
  <key>ProgramArguments</key>
  <array>
    <string>` + p.binary + `</string>
    <string>--config</string>
    <string>` + p.configFile + `</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict><key>HOME</key><string>` + p.home + `</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>` + p.logFile + `</string>
  <key>StandardErrorPath</key><string>` + p.logFile + `</string>
  <key>ProcessType</key><string>Background</string>
</dict>
</plist>
`
	}
	// StartLimit* live in [Unit], not [Service]: systemd moved them in v229 and
	// deprecates them in [Service], where they can be ignored outright — silently
	// voiding the crash-loop throttle they exist to provide. It gives up rather than
	// loop forever, leaving a failed unit that `systemctl --user status` reports.
	//
	// No After=network-online.target: that target is not part of a user manager, and
	// every listener here is loopback, so ordering against the network would be a
	// dependency that never arrives.
	return `[Unit]
Description=Cortex local proxy (authbridge-proxy)
Documentation=https://github.com/rossoctl/cortex
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
ExecStart=` + p.binary + ` --config ` + p.configFile + `
Restart=on-failure
RestartSec=10
StandardOutput=append:` + p.logFile + `
StandardError=append:` + p.logFile + `

[Install]
WantedBy=default.target
`
}

func loadService(p servicePaths) error {
	if runtime.GOOS == "darwin" {
		uid := strconv.Itoa(os.Getuid())
		// bootout first so a reinstall replaces cleanly; ignore its error, the
		// service may not be loaded at all.
		_ = exec.Command("launchctl", "bootout", "gui/"+uid+"/"+launchdLabel).Run() //nolint:errcheck
		out, err := exec.Command("launchctl", "bootstrap", "gui/"+uid, p.unitFile).CombinedOutput()
		if err != nil {
			return fmt.Errorf("launchctl bootstrap failed: %v: %s", err, strings.TrimSpace(string(out)))
		}
		return nil
	}
	if _, err := exec.LookPath("systemctl"); err != nil {
		return fmt.Errorf("systemctl not found — this system has no systemd (WSL1 or a container?).\n" +
			"  Remove the unit file and start the proxy yourself instead")
	}
	if out, err := exec.Command("systemctl", "--user", "daemon-reload").CombinedOutput(); err != nil {
		return fmt.Errorf("systemctl --user daemon-reload: %v: %s", err, strings.TrimSpace(string(out)))
	}
	if out, err := exec.Command("systemctl", "--user", "enable", "--now", systemdUnit).CombinedOutput(); err != nil {
		return fmt.Errorf("systemctl --user enable --now %s: %v: %s", systemdUnit, err, strings.TrimSpace(string(out)))
	}
	// Without lingering, a user unit stops at logout — which defeats the point on a
	// headless or SSH-only box. Best-effort: it needs polkit on some systems.
	if u := os.Getenv("USER"); u != "" {
		_ = exec.Command("loginctl", "enable-linger", u).Run() //nolint:errcheck
	}
	return nil
}

func unloadService(p servicePaths) error {
	if runtime.GOOS == "darwin" {
		uid := strconv.Itoa(os.Getuid())
		if out, err := exec.Command("launchctl", "bootout", "gui/"+uid+"/"+launchdLabel).CombinedOutput(); err != nil {
			return fmt.Errorf("launchctl bootout: %v: %s", err, strings.TrimSpace(string(out)))
		}
		return nil
	}
	if _, err := exec.LookPath("systemctl"); err != nil {
		return nil // nothing could have been loaded
	}
	if out, err := exec.Command("systemctl", "--user", "disable", "--now", systemdUnit).CombinedOutput(); err != nil {
		return fmt.Errorf("systemctl --user disable --now %s: %v: %s", systemdUnit, err, strings.TrimSpace(string(out)))
	}
	return nil
}

// runningPID returns the pid from a pidfile only when it is alive AND is one of
// ours. Same narrow check install.sh uses: the name is truncated to 15 characters
// on Linux, so match a prefix rather than the full 16-character name.
func runningPID(pidFile string) int {
	b, err := os.ReadFile(pidFile) //nolint:gosec // operator-supplied path
	if err != nil {
		return 0
	}
	pid, err := strconv.Atoi(strings.TrimSpace(string(b)))
	if err != nil || pid <= 0 {
		return 0
	}
	if err := syscall.Kill(pid, 0); err != nil {
		return 0
	}
	out, err := exec.Command("ps", "-p", strconv.Itoa(pid), "-o", "comm=").Output()
	if err != nil || !strings.Contains(string(out), "authbridge-prox") {
		return 0 // pid recycled onto something else
	}
	return pid
}

// stopPID asks politely, then waits. The proxy allows itself 15s to drain, so
// this waits longer than that before giving up rather than reporting success on a
// process that still holds the ports.
func stopPID(pid int) error {
	if err := syscall.Kill(pid, syscall.SIGTERM); err != nil {
		return err
	}
	for i := 0; i < 90; i++ {
		if err := syscall.Kill(pid, 0); err != nil {
			return nil
		}
		time.Sleep(200 * time.Millisecond)
	}
	return fmt.Errorf("still alive after 18s")
}

func waitHealthy(url string, within time.Duration) bool {
	c := &http.Client{Timeout: 2 * time.Second}
	deadline := time.Now().Add(within)
	for time.Now().Before(deadline) {
		resp, err := c.Get(url) //nolint:noctx // bounded by Timeout
		if err == nil {
			_ = resp.Body.Close()
			if resp.StatusCode >= 200 && resp.StatusCode < 300 {
				return true
			}
		}
		time.Sleep(500 * time.Millisecond)
	}
	return false
}

func lastLines(path string, n int) []string {
	f, err := os.Open(path) //nolint:gosec // operator-supplied path
	if err != nil {
		return []string{"(no log at " + path + ")"}
	}
	defer f.Close()
	var ring []string
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 0, 64*1024), 1024*1024)
	for sc.Scan() {
		ring = append(ring, sc.Text())
		if len(ring) > n {
			ring = ring[1:]
		}
	}
	if len(ring) == 0 {
		return []string{"(log is empty)"}
	}
	return ring
}

// dialableAddr turns a bind address into one a client can connect to. Named apart
// from the existing dialable() predicate in local_endpoint.go, which answers a
// different question.
func dialableAddr(addr string) string {
	host, port, err := net.SplitHostPort(addr)
	if err != nil {
		return addr
	}
	if host == "" || host == "0.0.0.0" || host == "::" {
		host = "localhost"
	}
	return net.JoinHostPort(host, port)
}
