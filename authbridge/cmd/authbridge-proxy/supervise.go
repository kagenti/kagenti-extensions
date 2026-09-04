package main

import (
	"errors"
	"fmt"
	"log/slog"
	"os"
	"os/exec"
	"os/signal"
	"slices"
	"syscall"
	"time"
)

// Supervision exists because launchd cannot be relied on for it.
//
// Measured on macOS 26.5.2, in a gui/<uid> domain with an active Aqua session, for
// an agent bootstrapped mid-session: launchd logs "pending spawn, domain in
// on-demand-only mode" and then honours NOTHING that would restart the process —
// KeepAlive=true, KeepAlive={SuccessfulExit:false}, StartInterval, and RunAtLoad all
// fail to fire, via both `launchctl bootstrap` and legacy `launchctl load -w`, with
// and without ProcessType, with the executable approved in Login Items. Bootstrapping
// into user/<uid> instead is refused outright. Only an explicit `launchctl kickstart`
// starts the job.
//
// So the process that restarts the proxy has to be one launchd already started. This
// is that process: launchd starts the supervisor (at login, which does work, or via
// kickstart at install), and the supervisor restarts the proxy for as long as it
// lives. A crash of the proxy — the failure this feature is for — is then covered
// without depending on launchd's autonomous spawn at all.
//
// What this does NOT cover: the supervisor itself being killed. Nothing restarts it
// until the next login. That is a far narrower gap than every crash going unrecovered,
// and it is stated rather than papered over.
const (
	// superviseRestartDelay is the pause after a child exits before starting another.
	superviseRestartDelay = 2 * time.Second
	// superviseMaxDelay caps the backoff when the child keeps dying immediately, so a
	// config that cannot start does not spin.
	superviseMaxDelay = 30 * time.Second
	// superviseHealthyRun is how long a child must survive for its run to count as
	// healthy, resetting the backoff.
	superviseHealthyRun = 10 * time.Second
)

// runSupervisor re-executes this binary without the supervise flag and restarts it
// whenever it exits, until the supervisor is asked to stop.
func runSupervisor(flagName string) error {
	self, err := os.Executable()
	if err != nil {
		return fmt.Errorf("locating this binary: %w", err)
	}
	// Same arguments, minus the flag that got us here, so the child runs the proxy.
	args := make([]string, 0, len(os.Args)-1)
	for _, a := range os.Args[1:] {
		if a == "-"+flagName || a == "--"+flagName {
			continue
		}
		args = append(args, a)
	}
	if slices.Contains(args, "-"+flagName) || slices.Contains(args, "--"+flagName) {
		return errors.New("internal: supervise flag survived argument filtering")
	}

	// Forward termination to the child so `launchctl bootout` / `systemctl stop`
	// takes the whole tree down; otherwise the proxy is orphaned and keeps the ports,
	// which would look exactly like the "stop that does not stop" this replaces.
	stopping := make(chan os.Signal, 1)
	signal.Notify(stopping, syscall.SIGTERM, syscall.SIGINT)

	delay := superviseRestartDelay
	for {
		cmd := exec.Command(self, args...) //nolint:gosec // our own binary, filtered args
		cmd.Stdout, cmd.Stderr, cmd.Stdin = os.Stdout, os.Stderr, nil
		start := time.Now()
		if err := cmd.Start(); err != nil {
			return fmt.Errorf("starting the proxy: %w", err)
		}
		slog.Info("supervisor: proxy started", "pid", cmd.Process.Pid)

		done := make(chan error, 1)
		go func() { done <- cmd.Wait() }()

		select {
		case sig := <-stopping:
			slog.Info("supervisor: stopping, forwarding signal to the proxy", "signal", sig.String())
			_ = cmd.Process.Signal(syscall.SIGTERM) //nolint:errcheck
			select {
			case <-done:
			case <-time.After(20 * time.Second):
				// Longer than the proxy's own 15s shutdown deadline, then insist.
				slog.Warn("supervisor: proxy did not exit in 20s; killing it")
				_ = cmd.Process.Kill() //nolint:errcheck
				<-done
			}
			return nil

		case werr := <-done:
			ran := time.Since(start)
			if ran >= superviseHealthyRun {
				delay = superviseRestartDelay // it was up long enough; forget the backoff
			}
			slog.Warn("supervisor: proxy exited; restarting",
				"ran", ran.Round(time.Millisecond), "err", werr, "restart_in", delay)
			select {
			case sig := <-stopping:
				slog.Info("supervisor: stopping instead of restarting", "signal", sig.String())
				return nil
			case <-time.After(delay):
			}
			if ran < superviseHealthyRun {
				if delay *= 2; delay > superviseMaxDelay {
					delay = superviseMaxDelay
				}
			}
		}
	}
}
