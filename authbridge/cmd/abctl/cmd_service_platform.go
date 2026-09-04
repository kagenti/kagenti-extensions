package main

import (
	"bufio"
	"bytes"
	"encoding/xml"
	"errors"
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
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
//   - launchd gets unconditional KeepAlive; systemd gets Restart=on-failure.
//     KeepAlive=true is the simpler guarantee for the requirement that matters —
//     come back after a crash — and it costs nothing, because the way to stop this
//     deliberately is `service uninstall`, which boots the job out rather than
//     leaning on a KeepAlive condition. Apple's {SuccessfulExit:false} form is
//     documented in terms of EXIT STATUS, which makes its behaviour on death by
//     signal ambiguous; there is no reason to depend on resolving that.
//
//     systemd needs no such trade: on-failure covers signal death, and a
//     `systemctl stop` is distinguishable from a crash, so a stop stays stopped.
//
//     NOT verified end to end: restart-after-crash could not be observed from the
//     session this was developed in. launchd logged "pending spawn, domain in
//     on-demand-only mode", i.e. it queued the respawn rather than running it —
//     a property of a headless context, not of this plist. Worth one manual
//     `kill -9` from a real GUI login before trusting it.
//
//   - A throttle. A config error is fatal at startup, so without one a bad edit
//     becomes a tight respawn loop.
func renderUnit(p servicePaths) string { return renderUnitFor(runtime.GOOS, p) }

// renderUnitFor takes the OS explicitly so both renderings are testable from
// either platform. Without it the systemd unit would be written on a Mac and
// never exercised until a Linux user hit it.
// xmlStr renders a plist <string> with its content XML-escaped. $HOME and a
// --config path are user-supplied: an "&" or "<" in either produces a plist that
// launchctl bootstrap rejects as malformed.
func xmlStr(v string) string {
	var b strings.Builder
	_ = xml.EscapeText(&b, []byte(v))
	return b.String()
}

// shQuote single-quotes a systemd ExecStart argument. systemd splits ExecStart on
// whitespace, so an unquoted path containing a space becomes two wrong arguments.
func shQuote(v string) string {
	return "'" + strings.ReplaceAll(v, "'", `'\''`) + "'"
}

// renderUnitFor builds the unit for goos.
//
// The macOS plist runs the proxy under --supervise. launchd will not restart these
// agents itself: in a gui/<uid> domain an agent bootstrapped mid-session gets
// "pending spawn, domain in on-demand-only mode", and neither KeepAlive nor
// StartInterval nor RunAtLoad fires (measured on macOS 26.5.2, active Aqua session,
// approved in Login Items, via both bootstrap and legacy load -w). Only kickstart
// starts it. So launchd starts a supervisor and the supervisor restarts the proxy.
// KeepAlive stays in the plist for the cases launchd does honour, such as a
// login-time load; it is no longer what crash recovery depends on.
//
// The systemd unit deliberately does NOT use --supervise: Restart=on-failure works
// there and counts restarts against StartLimit, which a nested supervisor would hide.
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
    <string>` + xmlStr(p.binary) + `</string>
    <string>--supervise</string>
    <string>--config</string>
    <string>` + xmlStr(p.configFile) + `</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict><key>HOME</key><string>` + xmlStr(p.home) + `</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>AbctlVersion</key><string>` + xmlStr(version) + `</string>
  <key>StandardOutPath</key><string>` + xmlStr(p.logFile) + `</string>
  <key>StandardErrorPath</key><string>` + xmlStr(p.logFile) + `</string>
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
X-AbctlVersion=` + version + `
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
ExecStart=` + shQuote(p.binary) + ` --config ` + shQuote(p.configFile) + `
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
		// Clear any disable left by `service stop`: a disabled label cannot be
		// bootstrapped, so without this a stop would make every later start and
		// install fail for a reason nothing on screen explains.
		_ = exec.Command("launchctl", "enable", "gui/"+uid+"/"+launchdLabel).Run() //nolint:errcheck
		// bootout first so a reinstall replaces cleanly; ignore its error, the
		// service may not be loaded at all.
		_ = exec.Command("launchctl", "bootout", "gui/"+uid+"/"+launchdLabel).Run() //nolint:errcheck

		// WAIT for it to actually leave the domain. `launchctl bootout` returns before
		// teardown finishes, and bootstrapping into a domain that still holds the label
		// fails with "Bootstrap failed: 5: Input/output error" — which is what a real
		// upgrade produced: the running service could not be replaced, the install
		// rolled back, and Cortex was left stopped.
		//
		// Our teardown is slow on purpose: bootout SIGTERMs the supervisor, which
		// forwards to the proxy and waits out its 15s graceful shutdown before
		// insisting. A trivial job dies fast enough to hide this, which is why every
		// test that started from nothing or ran uninstall first passed.
		if !waitBootedOut("gui/"+uid+"/"+launchdLabel, serviceBootoutTimeout) {
			return fmt.Errorf("the previous %s is still shutting down after %s; "+
				"run `abctl service status`, then try again", supervisorName(), serviceBootoutTimeout)
		}

		// Retried on EIO: the wait above closes the window, and this closes the race
		// inside it. Bootstrap is idempotent from our side — the label is not in the
		// domain, or we would not be here.
		var out []byte
		var err error
		for attempt := 1; ; attempt++ {
			out, err = exec.Command("launchctl", "bootstrap", "gui/"+uid, p.unitFile).CombinedOutput()
			if err == nil {
				break
			}
			if attempt >= 3 || !strings.Contains(string(out), "Input/output error") {
				return fmt.Errorf("launchctl bootstrap failed: %v: %s", err, strings.TrimSpace(string(out)))
			}
			time.Sleep(time.Duration(attempt) * time.Second)
		}
		// bootstrap REGISTERS the job; it does not reliably start it. Observed on a
		// real install: the agent loaded, `state = not running`, nothing served, and
		// Claude Code was broken until an explicit kickstart — RunAtLoad
		// notwithstanding. launchd's log said "pending spawn, domain in
		// on-demand-only mode", so whether a given session spawns at bootstrap
		// depends on the domain's state. Start it deliberately instead of depending
		// on that.
		if out, err := exec.Command("launchctl", "kickstart", "-p", "gui/"+uid+"/"+launchdLabel).CombinedOutput(); err != nil {
			return fmt.Errorf("launchctl kickstart failed: %v: %s", err, strings.TrimSpace(string(out)))
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
	//
	// uid, not $USER: a supervisor environment may not set USER at all, and the uid is
	// already what every other call here uses. loginctl accepts either.
	//
	// Record it only when WE turn it on, so uninstall can undo exactly that. Linger is
	// a per-user setting shared with every other user unit, so disabling it
	// unconditionally on uninstall could stop services that have nothing to do with
	// Cortex.
	uid := strconv.Itoa(os.Getuid())
	if !lingerEnabled(uid) {
		if err := exec.Command("loginctl", "enable-linger", uid).Run(); err != nil {
			// Not fatal — the unit is loaded and serving — but it does mean the service
			// stops at logout, so it must not be reported as surviving one. polkit
			// refuses this on some systems.
			return errLingerUnavailable
		}
		_ = os.WriteFile(lingerMarker(p), []byte("enabled by abctl\n"), 0o600) //nolint:errcheck
	}
	return nil
}

// errLingerUnavailable means the unit loaded but will not outlive a logout.
var errLingerUnavailable = errors.New(
	"loaded, but `loginctl enable-linger` failed: the service will stop when you log out. " +
		"Ask an administrator to run: loginctl enable-linger $USER")

// lingerMarker records that we enabled lingering, so uninstall undoes only that.
func lingerMarker(p servicePaths) string {
	return filepath.Join(filepath.Dir(p.configFile), "linger-enabled-by-abctl")
}

// lingerEnabled reports whether lingering is already on. A parse failure reads as
// "already on", which errs toward leaving the user's setting alone.
func lingerEnabled(uid string) bool {
	out, err := exec.Command("loginctl", "show-user", uid, "--property=Linger").Output()
	if err != nil {
		return true
	}
	return !strings.Contains(strings.ToLower(string(out)), "linger=no")
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
	// Undo lingering only if we are the ones who enabled it.
	if marker := lingerMarker(p); marker != "" {
		if _, serr := os.Stat(marker); serr == nil {
			_ = exec.Command("loginctl", "disable-linger", strconv.Itoa(os.Getuid())).Run() //nolint:errcheck
			_ = os.Remove(marker)                                                           //nolint:errcheck
		}
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

// controlService maps stop/start/restart onto the platform's supervisor.
func controlService(action string, p servicePaths) error {
	if runtime.GOOS == "darwin" {
		target := "gui/" + strconv.Itoa(os.Getuid()) + "/" + launchdLabel
		switch action {
		case "stop":
			// Two steps, because either alone is not a stop.
			//
			// `launchctl stop` is undone by KeepAlive within seconds. bootout removes
			// the job from the running domain — but the plist stays in
			// ~/Library/LaunchAgents, which launchd bootstraps again at next login, and
			// RunAtLoad then starts Cortex right back up. A "stop" that quietly undoes
			// itself when you log in is worse than no stop command, so pair it with
			// launchctl disable, which persists in the per-user disabled database.
			out, err := exec.Command("launchctl", "bootout", target).CombinedOutput()
			if err != nil && !strings.Contains(string(out), "No such process") {
				return fmt.Errorf("launchctl bootout: %v: %s", err, strings.TrimSpace(string(out)))
			}
			if out, derr := exec.Command("launchctl", "disable", target).CombinedOutput(); derr != nil {
				return fmt.Errorf("launchctl disable (stop would not survive a login): %v: %s",
					derr, strings.TrimSpace(string(out)))
			}
			return nil
		case "start":
			return loadService(p) // loadService clears the disable
		default: // restart
			_ = exec.Command("launchctl", "bootout", target).Run() //nolint:errcheck
			return loadService(p)
		}
	}
	if _, err := exec.LookPath("systemctl"); err != nil {
		return fmt.Errorf("systemctl not found; this system has no systemd")
	}
	// `systemctl --user stop` leaves the unit ENABLED, so it comes back at the next
	// boot exactly as launchd's plist does. Use the persistent forms for stop/start so
	// both platforms mean the same thing by those words; restart stays transient.
	args := []string{"--user", action, systemdUnit}
	switch action {
	case "stop":
		args = []string{"--user", "disable", "--now", systemdUnit}
	case "start":
		args = []string{"--user", "enable", "--now", systemdUnit}
	}
	if out, err := exec.Command("systemctl", args...).CombinedOutput(); err != nil {
		return fmt.Errorf("systemctl %s: %v: %s", strings.Join(args, " "), err, strings.TrimSpace(string(out)))
	}
	return nil
}

// supervisorRunning asks the supervisor whether OUR job is up, which health alone
// cannot establish: an unadopted proxy keeps the ports, the supervised copy
// crash-loops on the bind, and the probe succeeds against the survivor.
func supervisorRunning(p servicePaths) (bool, string) {
	if runtime.GOOS == "darwin" {
		target := "gui/" + strconv.Itoa(os.Getuid()) + "/" + launchdLabel
		// Poll rather than sample once. Immediately after a kickstart the job passes
		// through transient states — "xpcproxy" while launchd's exec helper is still
		// in the middle of the exec — and reading one of those as a verdict failed an
		// install whose service was merely still starting.
		deadline := time.Now().Add(serviceReadyTimeout)
		last := "no state reported"
		for {
			out, err := exec.Command("launchctl", "print", target).CombinedOutput()
			if err != nil {
				last = "launchctl print: job not found"
			} else {
				for _, line := range strings.Split(string(out), "\n") {
					line = strings.TrimSpace(line)
					if strings.HasPrefix(line, "state = ") {
						st := strings.TrimPrefix(line, "state = ")
						if st == "running" {
							return true, "state = running"
						}
						last = "state = " + st
						break
					}
				}
			}
			if time.Now().After(deadline) {
				return false, last
			}
			time.Sleep(300 * time.Millisecond)
		}
	}
	if _, err := exec.LookPath("systemctl"); err != nil {
		return true, "" // no systemd to ask; do not block on a check we cannot make
	}
	out, err := exec.Command("systemctl", "--user", "is-active", systemdUnit).Output()
	st := strings.TrimSpace(string(out))
	if err != nil && st == "" {
		return false, "systemctl is-active gave no answer"
	}
	return st == "active", "is-active = " + st
}

// establishedConns counts live TCP connections to addr's port, best-effort.
//
// Stopping the proxy cannot be made graceful from this side: HTTPS_PROXY is baked
// into each Claude Code process's environment at startup, so a running session has
// no way to fall back to a direct connection and `claude-code disable` cannot reach
// it. The stop therefore breaks every in-flight session, and the failure surfaces as
// a bare CURLE_COULDNT_CONNECT that looks like a Cortex bug. Naming the number of
// attached clients first turns that into a five-second diagnosis.
//
// Returns -1 when it cannot tell (no lsof), so the caller can stay silent rather
// than claim zero.
func establishedConns(addr string) int {
	if addr == "" {
		return -1
	}
	_, port, err := net.SplitHostPort(addr)
	if err != nil {
		return -1
	}
	if _, err := exec.LookPath("lsof"); err != nil {
		return -1
	}
	cmd := exec.Command("lsof", "-nP", "-iTCP:"+port, "-sTCP:ESTABLISHED")
	var errBuf bytes.Buffer
	cmd.Stderr = &errBuf
	out, err := cmd.Output()
	if err != nil {
		// lsof exits 1 both when nothing matches and on real failures (permission,
		// for one). Only the silent case is a true zero; anything that complained on
		// stderr is unknown, because reporting "0 attached" when we could not look
		// would be a confident wrong answer.
		if len(out) == 0 && errBuf.Len() == 0 {
			return 0
		}
		return -1
	}
	n := 0
	for _, line := range strings.Split(strings.TrimSpace(string(out)), "\n") {
		if line == "" || strings.HasPrefix(line, "COMMAND") {
			continue
		}
		n++
	}
	return n
}

// rotateLog keeps one previous generation once the log passes maxBytes.
//
// One unrotated file on a permanently-on service is a slow leak on its own — the
// proxy logs a line per inference request and response — and a fatal config error
// turns it into a fast one: unconditional KeepAlive with ThrottleInterval 10 means
// roughly six restarts a minute, each writing its startup banner. Rotating at
// start-of-run bounds exactly that case, because every one of those restarts passes
// through here.
func rotateLog(path string, maxBytes int64) {
	fi, err := os.Stat(path)
	if err != nil || fi.Size() <= maxBytes {
		return
	}
	// Rename, not truncate: the supervisor opens the path per spawn, so the next
	// start gets a fresh file while anything still holding the old inode keeps
	// writing there harmlessly.
	_ = os.Rename(path, path+".1") //nolint:errcheck
}

// unitWriterVersion reports which abctl wrote the installed unit, or "" if it
// carries no stamp (written before stamping existed).
//
// The unit outlives the tool that manages it. A newer build can write a plist while
// an older abctl stays on PATH, and that older one answers `service status` with
// "unknown subcommand" — so the launchd artifact is live and unmanageable, with no
// hint anywhere that the two disagree. Observed on a real machine.
func unitWriterVersion(unitFile string) string {
	b, err := os.ReadFile(unitFile) //nolint:gosec // path we wrote
	if err != nil {
		return ""
	}
	body := string(b)
	for _, marker := range []string{"<key>AbctlVersion</key><string>", "X-AbctlVersion="} {
		i := strings.Index(body, marker)
		if i < 0 {
			continue
		}
		rest := body[i+len(marker):]
		end := strings.IndexAny(rest, "<\n")
		if end < 0 {
			continue
		}
		return strings.TrimSpace(rest[:end])
	}
	return ""
}

// waitBootedOut polls until the label is gone from its domain.
//
// `launchctl bootout` is asynchronous: it returns while teardown is still in progress,
// and a bootstrap issued in that window fails with EIO. Polling `launchctl print` is
// the only signal available — a non-zero exit means the label is no longer there.
func waitBootedOut(target string, d time.Duration) bool {
	deadline := time.Now().Add(d)
	for {
		if err := exec.Command("launchctl", "print", target).Run(); err != nil {
			return true // no longer in the domain
		}
		if time.Now().After(deadline) {
			return false
		}
		time.Sleep(250 * time.Millisecond)
	}
}
