package main

import (
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"sort"
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
	// maxLogBytes bounds one generation of proxy.log; rotateLog keeps one previous
	// file, so the pair tops out near twice this.
	maxLogBytes  = 8 << 20
	serviceUsage = `abctl service — keep Cortex running across crashes and logins

Usage:
  abctl service install   [--yes] [--config PATH]
  abctl service uninstall [--yes]
  abctl service status
  abctl service stop | start | restart

install hands the proxy to the OS supervisor — a launchd user agent on macOS, a
systemd user unit on Linux — so it restarts on failure and comes back at login.
Claude Code depends on the proxy being up once "abctl claude-code enable" has run,
and nothing else keeps it up.

stop/start/restart exist so there is never a reason to reach for launchctl,
systemctl, kill or pkill: under a supervisor a plain kill is undone within seconds,
which looks like the process refusing to die.

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
	// forwardAddr is the proxy port clients point at, used only to count attached
	// sessions before a stop.
	forwardAddr string
	// configErr is why the config would not load, if it would not. Carried rather
	// than returned so uninstall and status still work on a broken config.
	configErr error
	home      string // set explicitly: a supervisor's environment is minimal
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
	// Deliberately NOT an error here. resolveServicePaths runs for every action, and
	// uninstall/status are exactly what you reach for when the binary is gone or
	// renamed — refusing them at that point leaves a loaded unit with no way to
	// remove or diagnose it. serviceInstall validates the binary itself, since it is
	// the only action that needs one.
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

	p.healthURL = resolveHealthURL(cortexCfg)
	// Kept, not discarded: install must refuse a config that cannot load, or it
	// writes the unit, gets an empty healthURL, skips the probe and calls a proxy
	// that cannot start "running". uninstall and status stay usable regardless,
	// which is the whole point of not returning it as an error here.
	if cfg, cerr := config.Load(cortexCfg); cerr != nil {
		p.configErr = cerr
	} else {
		config.ApplyPreset(cfg)
		p.forwardAddr = cfg.Listener.ForwardProxyAddr
	}
	return p, nil
}

// resolveHealthURL reads the health endpoint out of the config.
//
// ApplyPreset is applied because the binaries do: without it an unpinned
// health_addr reads as empty, so a config that never received the health pin
// produced no URL at all — and install then reported "Running under launchd" having
// probed nothing, for exactly the older configs that most need checking.
func resolveHealthURL(cortexCfg string) string {
	cfg, err := config.Load(cortexCfg)
	if err != nil {
		return ""
	}
	config.ApplyPreset(cfg)
	if cfg.Listener.HealthAddr == "" {
		return ""
	}
	return "http://" + dialableAddr(cfg.Listener.HealthAddr) + "/healthz"
}

func serviceInstalled(p servicePaths) bool {
	_, err := os.Stat(p.unitFile)
	return err == nil
}

func serviceInstall(p servicePaths, yes bool, stdout, stderr io.Writer) int {
	if _, err := os.Stat(p.configFile); err != nil {
		// Not "run the installer first": the installer is what calls this, so that
		// advice sent people in a circle. Name the command that creates the file.
		fmt.Fprintf(stderr, "abctl: no config at %s. Create it with:\n"+
			"  authbridge-proxy --local --write-config\n", p.configFile)
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

	if _, serr := os.Stat(p.binary); serr != nil {
		fmt.Fprintf(stderr, "abctl: authbridge-proxy not found at %s; install it first\n", p.binary)
		return 1
	}
	if p.configErr != nil {
		fmt.Fprintf(stderr, "abctl: %s will not load, so a supervised proxy could not start:\n  %v\n"+
			"  Fix it (or delete it and run: authbridge-proxy --local --write-config), then re-run.\n",
			p.configFile, p.configErr)
		return 1
	}

	// Taking ownership of how the proxy runs is the natural moment to bring an
	// older config up to date: the service starts it with --config, which honours
	// listeners that --local skipped, so an unpinned transparent_proxy_addr would
	// start binding every interface precisely now.
	if changed, mErr := migrateConfig(p.configFile, stdout); mErr != nil {
		// Refuse only if continuing would actually expose something. A migration that
		// fails on a config already bound to loopback costs nothing to skip; one that
		// fails on a config with wildcard listeners would supervise a proxy publishing
		// on every interface, which is not a thing to do quietly.
		if exposed := wildcardListeners(p.configFile); len(exposed) > 0 {
			fmt.Fprintf(stderr, "abctl: could not update %s (%v),\n"+
				"  and as it stands it binds %s on every interface.\n"+
				"  Refusing to supervise that. Add `bind_loopback_only: true` under listener:,\n"+
				"  or delete the file and run: authbridge-proxy --local --write-config\n",
				p.configFile, mErr, strings.Join(exposed, ", "))
			return 1
		}
		fmt.Fprintf(stderr, "abctl: could not update %s (%v); it already binds loopback only, continuing\n",
			p.configFile, mErr)
	} else if changed {
		// The paths were resolved from the pre-migration config, so anything the
		// migration added — health_addr above all — is not in them yet. Without this
		// the probe below is skipped for precisely the configs that were just fixed.
		p.healthURL = resolveHealthURL(p.configFile)
	}

	if adopt > 0 {
		fmt.Fprintf(stdout, "Stopping pid %d...\n", adopt)
		if err := stopPID(adopt); err != nil {
			fmt.Fprintf(stderr, "abctl: could not stop pid %d (%v); stop it yourself and re-run\n", adopt, err)
			return 1
		}
		_ = os.Remove(p.pidFile)
	}

	rotateLog(p.logFile, maxLogBytes)
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

	if err := loadService(p); errors.Is(err, errLingerUnavailable) {
		// The unit IS loaded, so this is a caveat rather than a failure: keep going,
		// but never claim it survives a logout.
		fmt.Fprintf(stderr, "abctl: %v\n", err)
	} else if err != nil {
		// Leaving the unit behind would make serviceInstalled() true for something
		// that never loaded, so `service status` would report it installed and
		// `service stop` would act on a job the supervisor does not have.
		if rmErr := os.Remove(p.unitFile); rmErr != nil && !os.IsNotExist(rmErr) {
			fmt.Fprintf(stderr, "abctl: also could not remove %s: %v\n", p.unitFile, rmErr)
		}
		fmt.Fprintf(stderr, "abctl: %v\n", err)
		return 1
	}

	// "Loaded" is not "serving". A bad config is fatal at startup and a supervisor
	// turns that into a restart loop, so probe the health endpoint before claiming
	// success.
	// Health alone is not proof: an unadopted proxy (started by hand, no pidfile)
	// keeps the ports, the supervised copy loses the bind race and crash-loops, and
	// the probe cheerfully succeeds against the survivor. Ask the supervisor whether
	// OUR job is actually up before believing the probe.
	if running, why := supervisorRunning(p); !running {
		fmt.Fprintf(stderr, "abctl: the unit loaded but the supervisor does not report it running (%s).\n"+
			"  Something else may hold the ports — check for a Cortex you started by hand:\n"+
			"    pgrep -fl authbridge-prox\n"+
			"  Last log lines:\n", why)
		for _, line := range lastLines(p.logFile, 5) {
			fmt.Fprintf(stderr, "    %s\n", line)
		}
		return 1
	}

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
	if runtime.GOOS == "darwin" {
		// launchd does not restart these agents — see the comment in the plist and in
		// cmd/authbridge-proxy/supervise.go — so the proxy runs under --supervise and
		// crash recovery belongs to that supervisor, not to KeepAlive.
		fmt.Fprintln(stdout, "  Crash recovery is handled by a supervisor process, because launchd")
		fmt.Fprintln(stdout, "  does not restart user agents added mid-session. Check it with:")
		fmt.Fprintln(stdout, "    kill -9 $(pgrep -f 'authbridge-proxy --config')  # back within ~2s")
	}
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
	// Skew is worth naming before anything else: if the unit was written by a
	// different abctl, the rest of this output describes a job this binary may not be
	// able to manage.
	if w := unitWriterVersion(p.unitFile); w != "" && w != version {
		fmt.Fprintf(stdout, "WARNING: this unit was written by abctl %s; you are running %s.\n"+
			"  The unit also pins a fixed authbridge-proxy path, which that abctl chose.\n"+
			"  Reinstall with this build to bring them back in step:  abctl service install\n",
			w, version)
	}

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
	// Counted BEFORE the stop, while the connections still exist.
	n := -1
	if action == "stop" {
		n = establishedConns(p.forwardAddr)
	}
	if action == "start" || action == "restart" {
		rotateLog(p.logFile, maxLogBytes)
	}
	if err := controlService(action, p); err != nil {
		fmt.Fprintf(stderr, "abctl: %v\n", err)
		return 1
	}
	switch action {
	case "stop":
		fmt.Fprintln(stdout, "Stopped, and it will stay stopped across logins.")
		fmt.Fprintln(stdout, "  abctl service start")
		if n > 0 {
			fmt.Fprintf(stdout, "\n  %d connection(s) were attached to %s and have just been cut.\n",
				n, p.forwardAddr)
			fmt.Fprintln(stdout, "  A running Claude Code cannot fall back to a direct connection —")
			fmt.Fprintln(stdout, "  HTTPS_PROXY is fixed in its environment at startup — so restart any")
			fmt.Fprintln(stdout, "  session that now fails to connect.")
		}
		return 0
	default:
		// Same gate install uses: an unadopted proxy holding the ports answers the
		// probe while OUR job crash-loops on the bind, so health alone would report a
		// restart that did not happen.
		if running, why := supervisorRunning(p); !running {
			fmt.Fprintf(stderr, "abctl: %sed, but the supervisor does not report it running (%s).\n"+
				"  Check for a Cortex started by hand holding the ports: pgrep -fl authbridge-prox\n", action, why)
			for _, line := range lastLines(p.logFile, 5) {
				fmt.Fprintf(stderr, "    %s\n", line)
			}
			return 1
		}
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

// wildcardListeners names the listener addresses that are not bound to loopback,
// so a refusal can say what is actually exposed.
func wildcardListeners(cortexCfg string) []string {
	cfg, err := config.Load(cortexCfg)
	if err != nil {
		return nil // unreadable: the caller's own error already covers it
	}
	config.ApplyPreset(cfg)
	var out []string
	for name, addr := range map[string]string{
		"forward_proxy_addr":     cfg.Listener.ForwardProxyAddr,
		"session_api_addr":       cfg.Listener.SessionAPIAddr,
		"health_addr":            cfg.Listener.HealthAddr,
		"transparent_proxy_addr": cfg.Listener.TransparentProxyAddr,
		"stats address":          cfg.Stats.StatsAddress,
	} {
		if addr == "" {
			continue
		}
		if host, _, serr := net.SplitHostPort(addr); serr == nil && !isLoopbackHost(host) {
			out = append(out, name)
		}
	}
	sort.Strings(out)
	return out
}

// isLoopbackHost treats an empty host as a wildcard, which is exactly what ":9091"
// means.
func isLoopbackHost(host string) bool {
	if host == "" {
		return false
	}
	if ip := net.ParseIP(host); ip != nil {
		return ip.IsLoopback()
	}
	return host == "localhost"
}
