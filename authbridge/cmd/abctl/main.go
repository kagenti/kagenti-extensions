// Command abctl is an interactive terminal UI for inspecting AuthBridge's
// in-memory session store.
//
// Default mode opens a Namespaces → Pods picker, port-forwards the
// selected pod, and renders the session-events view. Pass --endpoint
// to skip the picker and connect directly (the pre-picker behavior).
package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"os/exec"
	"os/signal"
	"strings"
	"syscall"

	"github.com/rossoctl/cortex/authbridge/cmd/abctl/cluster"
	"github.com/rossoctl/cortex/authbridge/cmd/abctl/edit"
	"github.com/rossoctl/cortex/authbridge/cmd/abctl/tui"
)

// version is the abctl build version, overridden at release time via
// -ldflags "-X main.version=<tag>". Defaults to "dev" for local builds.
var version = "dev"

func main() {
	// Subcommand dispatch happens before flag.Parse: a non-flag first
	// argument selects a subcommand, and anything else falls through to the
	// terminal UI, preserving the original flags-only invocation.
	if len(os.Args) > 1 && !strings.HasPrefix(os.Args[1], "-") {
		switch os.Args[1] {
		case "tools":
			os.Exit(runTools(os.Args[2:], os.Stdout, os.Stderr))
		case "claude-code":
			os.Exit(runClaudeCode(os.Args[2:], os.Stdout, os.Stderr))
		case "service":
			os.Exit(runService(os.Args[2:], os.Stdout, os.Stderr))
		default:
			fmt.Fprintf(os.Stderr, "abctl: unknown subcommand %q (known: tools, claude-code, service)\n", os.Args[1])
			os.Exit(2)
		}
	}

	endpoint := flag.String("endpoint", "",
		"AuthBridge session API URL (e.g. http://localhost:9094). When omitted, abctl connects to the Cortex on this machine if one is running, otherwise it opens a Namespaces → Pods picker.")
	showVersion := flag.Bool("version", false, "print version and exit")
	flag.Parse()

	if *showVersion {
		fmt.Println("abctl", version)
		return
	}

	// Best-effort sweep of edit-tempfiles older than 24h. Tempfiles are
	// intentionally left in place on every exit path (success / abort /
	// crash) so a user can recover an in-progress edit; the sweep keeps
	// $TMPDIR bounded for users who edit often.
	_ = edit.SweepStaleTempfiles()

	// With no --endpoint, prefer a Cortex running on this machine. Before this,
	// a bare `abctl` on a laptop demanded kubectl and opened a cluster picker,
	// so the local install — the whole quickstart — needed
	// `--endpoint http://localhost:47601` typed every time.
	//
	// Only when it is actually answering: a stale config from an install that is
	// no longer running must not hijack abctl away from the picker for someone
	// working against a cluster.
	local := localSessionEndpoint()
	localUp := localSessionAPIUp(local)
	if *endpoint == "" && localUp {
		*endpoint = local
	}

	// Friendly check: if picker mode and no kubectl, fail fast with a
	// clear message instead of a stack trace later.
	if *endpoint == "" {
		if _, err := exec.LookPath("kubectl"); err != nil {
			msg := "abctl: kubectl not found on PATH; install it or pass --endpoint http://..."
			// Name the more likely cause first when there is a local install that
			// simply is not running — "install kubectl" is unhelpful advice to
			// someone who has never wanted a cluster.
			if local != "" && !dialable(local) {
				msg = "abctl: nothing is listening on " + local + " (from ~/.cortex/config.yaml).\n" +
					"  Start it:  authbridge-proxy --config ~/.cortex/config.yaml &\n" +
					"  Or pass --endpoint http://... , or install kubectl to browse a cluster."
			}
			fmt.Fprintln(os.Stderr, msg)
			os.Exit(1)
		}
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	sigs := make(chan os.Signal, 1)
	signal.Notify(sigs, os.Interrupt, syscall.SIGTERM)
	go func() {
		<-sigs
		cancel()
	}()

	// LocalEndpoint only when it answered. Passing an unresponsive configured
	// address would point [l] at it and take away the in-cluster default, so a
	// working `kubectl port-forward` on 9094 could not be reached with the one key
	// that exists for exactly that.
	opts := tui.RunOptions{Endpoint: *endpoint}
	if localUp {
		opts.LocalEndpoint = local
	}
	if *endpoint == "" {
		opts.Lister = cluster.NewLister()
		opts.PortForwarder = cluster.NewPortForwarder()
	}
	if err := tui.Run(ctx, opts); err != nil {
		fmt.Fprintf(os.Stderr, "abctl: %v\n", err)
		os.Exit(1)
	}
}
