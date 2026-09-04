package main

import (
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"time"

	"github.com/rossoctl/cortex/authbridge/authlib/config"
)

// Once Claude Code's settings point at the proxy, every request goes there — so a
// proxy that is not running means Claude Code does not work. `nohup … &` survives
// neither a crash nor a logout, which makes "it worked yesterday" the normal
// experience after a reboot. These commands hand the process to the OS supervisor
// instead.
//
// User-scoped only, never a system daemon: this process holds the bridge CA's
// private key, and running it as root to gain "robustness" would be a bad trade.
const (
	launchdLabel = "io.rossoctl.cortex"
	systemdUnit  = "cortex.service"
	serviceUsage = `abctl service — keep Cortex running across crashes and logins

Usage:
  abctl service install   [--yes] [--config PATH]
  abctl service uninstall [--yes]
  abctl service status

install hands the proxy to the OS supervisor — a launchd user agent on macOS, a
systemd user unit on Linux — so it restarts on failure and comes back at login.
Claude Code depends on the proxy being up once "abctl claude-code enable" has run,
and nothing else keeps it up.

If a proxy you started by hand is already running, install stops it first and
supervises a fresh one: two copies cannot share the ports, and the supervised one
would otherwise crash-loop while the old one kept serving — broken in a way that
only shows up at your next reboot.

Never installed as root. The proxy holds a CA private key; this stays a user
service.
`
	// serviceReadyTimeout bounds the post-start health probe. The supervisor
	// reports "loaded", not "serving", and the difference is where a bad config
	// hides.
	serviceReadyTimeout = 15 * time.Second
)

// servicePaths is everything the platform-specific bits need, gathered so tests
// can point all of it at a temp dir instead of the real ~/Library/LaunchAgents.
type servicePaths struct {
	unitFile   string // plist or .service
	binary     string // absolute path to authbridge-proxy
	configFile string // ~/.cortex/config.yaml
	logFile    string
	pidFile    string
	healthURL  string
	home       string // set explicitly: a supervisor's environment is minimal
}

func runService(args []string, stdout, stderr io.Writer) int {
	if len(args) == 0 {
		fmt.Fprint(stderr, serviceUsage)
		return 2
	}
	action := args[0]

	fs := newFlagSet("service "+action, stderr)
	yes := fs.Bool("yes", false, "do not prompt for confirmation")
	cortexCfg := fs.String("config", "", "Cortex config file")
	unitOverride := fs.String("unit-file", "", "unit file path (testing)")
	printUnit := fs.Bool("print-unit", false, "print the unit file and exit, installing nothing")
	if err := fs.Parse(args[1:]); err != nil {
		return 2
	}

	p, err := resolveServicePaths(*cortexCfg, *unitOverride)
	if err != nil {
		fmt.Fprintf(stderr, "abctl: %v\n", err)
		return 1
	}

	if *printUnit {
		// Inspect before installing. Also the only way to exercise rendering without
		// registering a real agent in the caller's session.
		fmt.Fprint(stdout, renderUnit(p))
		return 0
	}

	switch action {
	case "install":
		return serviceInstall(p, *yes, stdout, stderr)
	case "uninstall":
		return serviceUninstall(p, *yes, stdout, stderr)
	case "status":
		return serviceStatus(p, stdout)
	case "stop", "start", "restart":
		return serviceControl(action, p, stdout, stderr)
	default:
		fmt.Fprintf(stderr, "abctl: unknown service action %q "+
			"(install, uninstall, status, stop, start, restart)\n", action)
		return 2
	}
}

// resolveServicePaths refuses early on an unsupported platform rather than
// writing a unit file that will never be honoured.
func resolveServicePaths(cortexCfg, unitOverride string) (servicePaths, error) {
	var p servicePaths
	home, err := os.UserHomeDir()
	if err != nil || home == "" {
		return p, fmt.Errorf("cannot determine your home directory (is $HOME set?)")
	}
	if cortexCfg == "" {
		cortexCfg = filepath.Join(home, ".cortex", "config.yaml")
	}
	p.home = home
	p.configFile = cortexCfg
	p.logFile = filepath.Join(filepath.Dir(cortexCfg), "proxy.log")
	p.pidFile = filepath.Join(filepath.Dir(cortexCfg), "proxy.pid")

	// An absolute binary path: a supervisor has no shell PATH to search.
	bin, err := exec.LookPath("authbridge-proxy")
	if err != nil {
		bin = filepath.Join(home, ".local", "bin", "authbridge-proxy")
	}
	if abs, aerr := filepath.Abs(bin); aerr == nil {
		bin = abs
	}
	if _, serr := os.Stat(bin); serr != nil {
		return p, fmt.Errorf("authbridge-proxy not found at %s; install it first", bin)
	}
	p.binary = bin

	switch unitOverride {
	case "":
		switch runtime.GOOS {
		case "darwin":
			p.unitFile = filepath.Join(home, "Library", "LaunchAgents", launchdLabel+".plist")
		case "linux":
			p.unitFile = filepath.Join(home, ".config", "systemd", "user", systemdUnit)
		default:
			return p, fmt.Errorf("no supervisor integration for %s; start the proxy yourself:\n"+
				"  authbridge-proxy --config %s &", runtime.GOOS, cortexCfg)
		}
	default:
		p.unitFile = unitOverride
	}

	// The health endpoint is the only thing that proves it is serving rather than
	// merely loaded, and it comes from the config so it cannot drift.
	if cfg, cerr := config.Load(cortexCfg); cerr == nil && cfg.Listener.HealthAddr != "" {
		p.healthURL = "http://" + dialableAddr(cfg.Listener.HealthAddr) + "/healthz"
	}
	return p, nil
}

func serviceInstalled(p servicePaths) bool {
	_, err := os.Stat(p.unitFile)
	return err == nil
}

func serviceInstall(p servicePaths, yes bool, stdout, stderr io.Writer) int {
	if _, err := os.Stat(p.configFile); err != nil {
		fmt.Fprintf(stderr, "abctl: no config at %s — run the installer first\n", p.configFile)
		return 1
	}

	fmt.Fprintf(stdout, "This will install a %s that runs:\n  %s --config %s\n\n",
		supervisorName(), p.binary, p.configFile)
	fmt.Fprintf(stdout, "It restarts on failure and starts at login, so Claude Code keeps working\n"+
		"after a crash or a reboot. Unit file: %s\n\n", p.unitFile)

	// Adopt rather than collide. Two copies cannot share the ports, and the
	// supervised one would lose the race and crash-loop while the hand-started one
	// kept serving — a break that only surfaces at the next reboot.
	adopt := runningPID(p.pidFile)
	if adopt > 0 {
		fmt.Fprintf(stdout, "A Cortex you started by hand is running (pid %d). It will be stopped\n"+
			"so the supervised one can take the ports.\n\n", adopt)
	}
	fmt.Fprintf(stdout, "Undo with: abctl service uninstall\n\n")
	if !yes && !confirm(stdout) {
		fmt.Fprintln(stdout, "Not changed.")
		return exitDeclined
	}

	// Taking ownership of how the proxy runs is the natural moment to bring an
	// older config up to date: the service starts it with --config, which honours
	// listeners that --local skipped, so an unpinned transparent_proxy_addr would
	// start binding every interface precisely now.
	if _, mErr := migrateConfig(p.configFile, stdout); mErr != nil {
		fmt.Fprintf(stderr, "abctl: could not update %s (%v); continuing with it as-is\n",
			p.configFile, mErr)
	}

	if adopt > 0 {
		fmt.Fprintf(stdout, "Stopping pid %d...\n", adopt)
		if err := stopPID(adopt); err != nil {
			fmt.Fprintf(stderr, "abctl: could not stop pid %d (%v); stop it yourself and re-run\n", adopt, err)
			return 1
		}
		_ = os.Remove(p.pidFile)
	}

	tightenLog(p.logFile, stderr)

	if err := os.MkdirAll(filepath.Dir(p.unitFile), 0o755); err != nil {
		fmt.Fprintf(stderr, "abctl: %v\n", err)
		return 1
	}
	if err := os.WriteFile(p.unitFile, []byte(renderUnit(p)), 0o644); err != nil {
		fmt.Fprintf(stderr, "abctl: writing %s: %v\n", p.unitFile, err)
		return 1
	}
	fmt.Fprintf(stdout, "Wrote %s\n", p.unitFile)

	if err := loadService(p); err != nil {
		fmt.Fprintf(stderr, "abctl: %v\n", err)
		return 1
	}

	// "Loaded" is not "serving". A bad config is fatal at startup and a supervisor
	// turns that into a restart loop, so probe the health endpoint before claiming
	// success.
	if p.healthURL != "" {
		if waitHealthy(p.healthURL, serviceReadyTimeout) {
			fmt.Fprintf(stdout, "\nRunning under %s and healthy. Claude Code will keep working across\n"+
				"crashes and logins.\n", supervisorName())
			return 0
		}
		fmt.Fprintf(stderr, "\nabctl: installed, but nothing answered %s within %s.\n"+
			"  Check %s — a config error is fatal at startup and the supervisor will keep retrying.\n"+
			"  abctl service status shows the current state.\n", p.healthURL, serviceReadyTimeout, p.logFile)
		return 1
	}
	fmt.Fprintf(stdout, "\nRunning under %s.\n", supervisorName())
	return 0
}

func serviceUninstall(p servicePaths, yes bool, stdout, stderr io.Writer) int {
	if !serviceInstalled(p) {
		fmt.Fprintf(stdout, "Nothing to do: no unit at %s\n", p.unitFile)
		return 0
	}
	fmt.Fprintf(stdout, "This will stop and remove the %s at:\n  %s\n\n", supervisorName(), p.unitFile)
	fmt.Fprintf(stdout, "Cortex will no longer start at login. Claude Code stops working whenever\n"+
		"the proxy is not running — `abctl claude-code disable` removes that dependency.\n\n")
	if !yes && !confirm(stdout) {
		fmt.Fprintln(stdout, "Not changed.")
		return exitDeclined
	}
	if err := unloadService(p); err != nil {
		// Report but keep going: leaving the unit file behind would make a
		// reinstall look installed-but-dead.
		fmt.Fprintf(stderr, "abctl: %v\n", err)
	}
	if err := os.Remove(p.unitFile); err != nil {
		fmt.Fprintf(stderr, "abctl: removing %s: %v\n", p.unitFile, err)
		return 1
	}
	fmt.Fprintf(stdout, "\nRemoved. Start it yourself with:\n  %s --config %s &\n", p.binary, p.configFile)
	return 0
}

func serviceStatus(p servicePaths, stdout io.Writer) int {
	if !serviceInstalled(p) {
		fmt.Fprintf(stdout, "not installed (%s)\n", p.unitFile)
		if pid := runningPID(p.pidFile); pid > 0 {
			fmt.Fprintf(stdout, "  a hand-started Cortex is running (pid %d); nothing restarts it\n", pid)
		}
		return 0
	}
	fmt.Fprintf(stdout, "installed: %s\n", p.unitFile)
	if p.healthURL == "" {
		return 0
	}
	if waitHealthy(p.healthURL, 2*time.Second) {
		fmt.Fprintf(stdout, "healthy: %s\n", p.healthURL)
		return 0
	}
	// Installed but not serving is the state worth naming loudly: Claude Code is
	// pointed at a proxy that is not answering.
	fmt.Fprintf(stdout, "NOT answering %s\n", p.healthURL)
	fmt.Fprintf(stdout, "  Claude Code will fail while this is true. Last log lines:\n")
	for _, line := range lastLines(p.logFile, 5) {
		fmt.Fprintf(stdout, "    %s\n", line)
	}
	return 1
}

// serviceControl is the whole reason users never need launchctl or systemctl: a
// plain kill against a supervised process is undone in seconds, which reads as the
// process refusing to die.
func serviceControl(action string, p servicePaths, stdout, stderr io.Writer) int {
	if !serviceInstalled(p) {
		fmt.Fprintf(stderr, "abctl: no service installed (%s). Install it with:\n"+
			"  abctl service install\n", p.unitFile)
		return 1
	}
	if err := controlService(action, p); err != nil {
		fmt.Fprintf(stderr, "abctl: %v\n", err)
		return 1
	}
	switch action {
	case "stop":
		fmt.Fprintln(stdout, "Stopped. Claude Code will fail until it is running again:")
		fmt.Fprintln(stdout, "  abctl service start")
		return 0
	default:
		if p.healthURL != "" && !waitHealthy(p.healthURL, serviceReadyTimeout) {
			fmt.Fprintf(stderr, "abctl: %sed, but nothing answered %s. Last log lines:\n", action, p.healthURL)
			for _, line := range lastLines(p.logFile, 5) {
				fmt.Fprintf(stderr, "    %s\n", line)
			}
			return 1
		}
		fmt.Fprintf(stdout, "%sed.\n", strings.ToUpper(action[:1])+action[1:])
		return 0
	}
}

// tightenLog makes the proxy log owner-only before the supervisor opens it.
//
// The supervisor creates it with its own umask — 0644 in practice — and it records
// every host the proxy talks to. Creating it first means the supervisor appends to a
// file that is already tight; the chmod also catches one an earlier install left
// loose. Best-effort throughout: a log mode is not worth failing an install over.
func tightenLog(path string, stderr io.Writer) {
	if f, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600); err == nil {
		_ = f.Close()
	}
	if err := os.Chmod(path, 0o600); err != nil && !os.IsNotExist(err) {
		fmt.Fprintf(stderr, "abctl: could not tighten %s (%v); continuing\n", path, err)
	}
}
