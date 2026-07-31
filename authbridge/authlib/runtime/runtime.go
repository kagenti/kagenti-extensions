// Package runtime holds process-level helpers shared by the authbridge
// binaries (authbridge-proxy, authbridge-cpex, authbridge-envoy). Each binary
// has its own main() orchestration and listener wiring; only the byte-identical
// plumbing — logging setup, the SIGUSR1 log-level toggle, the health and stats
// servers, and the HTTP-listener helpers — lives here so a fix has to be made
// once rather than three times.
//
// This package is intentionally listener-agnostic: it imports no gRPC and no
// cgo, so authlib does not gain the envoy (go-control-plane / grpc) or cpex
// (libcpex_ffi) dependency by hosting it.
package runtime

import (
	"log"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/rossoctl/cortex/authbridge/authlib/config"
	"github.com/rossoctl/cortex/authbridge/authlib/listener/reverseproxy"
	"github.com/rossoctl/cortex/authbridge/authlib/observe"
	"github.com/rossoctl/cortex/authbridge/authlib/pipeline"
)

// logLevel is the process-wide slog level, mutated live by StartSignalToggle.
var logLevel = new(slog.LevelVar)

// LogLevel returns the current process-wide slog level, for binaries that log
// their configured level at startup.
func LogLevel() slog.Level { return logLevel.Level() }

// InitLogging sets the process log level from the LOG_LEVEL env var (debug /
// warn / error, default info) and installs a slog text handler on stderr.
func InitLogging() {
	switch strings.ToLower(os.Getenv("LOG_LEVEL")) {
	case "debug":
		logLevel.Set(slog.LevelDebug)
	case "warn":
		logLevel.Set(slog.LevelWarn)
	case "error":
		logLevel.Set(slog.LevelError)
	default:
		logLevel.Set(slog.LevelInfo)
	}
	slog.SetDefault(slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: logLevel})))
}

// StartSignalToggle installs a SIGUSR1 handler that toggles the process log
// level between DEBUG and INFO, so operators can flip verbose logging on a
// running pod without a restart.
func StartSignalToggle() {
	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGUSR1)
	go func() {
		for range sigCh {
			if logLevel.Level() == slog.LevelDebug {
				logLevel.Set(slog.LevelInfo)
				slog.Info("log level toggled to INFO (send SIGUSR1 to switch back to DEBUG)")
			} else {
				logLevel.Set(slog.LevelDebug)
				slog.Info("log level toggled to DEBUG (send SIGUSR1 to switch back to INFO)")
			}
		}
	}()
}

// StartHealthServer serves liveness (/healthz) and readiness (/readyz) on :9091
// in a goroutine. Readiness reports 503 while any inbound or outbound plugin is
// still waiting on a dependency (e.g. a credential file that hasn't landed yet).
func StartHealthServer(inboundH, outboundH *pipeline.Holder) {
	go func() {
		mux := http.NewServeMux()
		mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(http.StatusOK)
		})
		mux.HandleFunc("/readyz", func(w http.ResponseWriter, r *http.Request) {
			if name := inboundH.NotReadyPlugin(); name != "" {
				http.Error(w, "inbound plugin not ready: "+name, http.StatusServiceUnavailable)
				return
			}
			if name := outboundH.NotReadyPlugin(); name != "" {
				http.Error(w, "outbound plugin not ready: "+name, http.StatusServiceUnavailable)
				return
			}
			w.WriteHeader(http.StatusOK)
		})
		slog.Info("health server listening", "addr", ":9091")
		if err := http.ListenAndServe(":9091", mux); err != nil {
			slog.Warn("health server failed", "error", err)
		}
	}()
}

// StartStatServer starts the stats/config-inspection server (default :9093) in
// a goroutine and returns it for graceful shutdown.
func StartStatServer(cfg *config.Config, cfgProvider observe.ConfigProvider, statsProvider observe.StatsProvider, reloadStatus http.Handler) *observe.StatServer {
	srv := observe.NewStatServer(cfg.Stats.StatsAddress, cfgProvider, statsProvider,
		observe.WithReloadStatus(reloadStatus))
	go func() {
		slog.Info("stat server listening", "addr", cfg.Stats.StatsAddress)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("stat server: %v", err)
		}
	}()
	return srv
}

// StartHTTPServer binds addr, serves handler in a goroutine, and returns the
// server for graceful shutdown. It logs the concrete bound address (resolving
// an ephemeral ":0" to the OS-assigned port). Bind failures are fatal.
func StartHTTPServer(name string, handler http.Handler, addr string) *http.Server {
	srv := &http.Server{
		Addr:              addr,
		Handler:           handler,
		ReadHeaderTimeout: 10 * time.Second,
	}
	listener, err := net.Listen("tcp", addr)
	if err != nil {
		log.Fatalf("%s listen: %v", name, err)
	}
	go func() {
		slog.Info("HTTP server listening", "name", name, "addr", listener.Addr().String())
		if err := srv.Serve(listener); err != nil && err != http.ErrServerClosed {
			log.Fatalf("%s serve: %v", name, err)
		}
	}()
	return srv
}

// StartReverseProxyServer mirrors StartHTTPServer but uses the
// reverseproxy.Server's Listen() method so the byte-peek TLS-sniffing
// listener is wired in when mTLS is enabled. With mTLS off, Listen
// returns a plain net.Listen and behavior matches StartHTTPServer.
//
// Logged "mtls" attribute makes the listener mode visible at startup;
// operators expecting a separate :8443 port for TLS get a clear hint
// that this is the same :8080 with byte-peek detection.
func StartReverseProxyServer(name string, rp *reverseproxy.Server, addr string) *http.Server {
	srv := &http.Server{
		Addr:              addr,
		Handler:           rp.Handler(),
		ReadHeaderTimeout: 10 * time.Second,
	}
	listener, err := rp.Listen(addr)
	if err != nil {
		log.Fatalf("%s listen: %v", name, err)
	}
	go func() {
		slog.Info("Reverse server listening", "name", name, "addr", listener.Addr().String(), "mtls", rp.MTLSEnabled())
		if err := srv.Serve(listener); err != nil && err != http.ErrServerClosed {
			log.Fatalf("%s serve: %v", name, err)
		}
	}()
	return srv
}
