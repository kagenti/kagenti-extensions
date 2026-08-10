//go:build cpex

// Package main is the CPEX-enabled authbridge binary: identical to
// authbridge-proxy (HTTP forward + reverse proxies, full rossoctl
// plugin set) plus the `cpex` plugin which routes hooks through the
// CPEX (Context Plugin Execution) framework — including the APL DSL
// and any pre-built CPEX policy plugins (Cedar, PII scanner, audit
// logger, etc.).
//
// This binary requires `-tags cpex` and links libcpex_ffi via cgo.
// The build constraint at the top of this file ensures a no-tag
// build fails fast rather than silently producing an authbridge-proxy
// duplicate.
//
// For envoy-sidecar mode use authbridge-envoy; for a no-cgo, pure-Go
// build use authbridge-proxy. The body of main() below is duplicated
// from authbridge-proxy/main.go pending an authlib-side `Run()`
// extraction — see this binary's README for the extraction proposal.
package main

import (
	"context"
	"flag"
	"fmt"
	"log"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/rossoctl/cortex/authbridge/authlib/auth"
	"github.com/rossoctl/cortex/authbridge/authlib/config"
	"github.com/rossoctl/cortex/authbridge/authlib/pipeline"
	"github.com/rossoctl/cortex/authbridge/authlib/plugins"
	"github.com/rossoctl/cortex/authbridge/authlib/reloader"
	"github.com/rossoctl/cortex/authbridge/authlib/runtimeutil"
	"github.com/rossoctl/cortex/authbridge/authlib/session"
	"github.com/rossoctl/cortex/authbridge/authlib/sessionapi"
	"github.com/rossoctl/cortex/authbridge/authlib/shared"
	"github.com/rossoctl/cortex/authbridge/authlib/spiffe"
	authtls "github.com/rossoctl/cortex/authbridge/authlib/tls"

	"github.com/rossoctl/cortex/authbridge/authlib/listener/forwardproxy"
	"github.com/rossoctl/cortex/authbridge/authlib/listener/reverseproxy"

	// Plugins — same set as authbridge-proxy, plus the cpex plugin
	// which lives behind //go:build cpex. The cpex import only fires
	// in this binary's build; pure-Go binaries (authbridge-proxy,
	// authbridge-envoy, authbridge-lite) don't import it.
	_ "github.com/rossoctl/cortex/authbridge/authlib/plugins/a2aparser"
	_ "github.com/rossoctl/cortex/authbridge/authlib/plugins/cpex"
	_ "github.com/rossoctl/cortex/authbridge/authlib/plugins/ibac"
	_ "github.com/rossoctl/cortex/authbridge/authlib/plugins/inferenceparser"
	_ "github.com/rossoctl/cortex/authbridge/authlib/plugins/jwtvalidation"
	_ "github.com/rossoctl/cortex/authbridge/authlib/plugins/mcpparser"
	_ "github.com/rossoctl/cortex/authbridge/authlib/plugins/sparc"
	_ "github.com/rossoctl/cortex/authbridge/authlib/plugins/tokenbroker"
	_ "github.com/rossoctl/cortex/authbridge/authlib/plugins/tokenexchange"
)

func main() {
	configPath := flag.String("config", "", "path to config YAML file")
	flag.Parse()

	runtimeutil.InitLogging("authbridge-cpex")
	runtimeutil.StartSignalToggle()

	if *configPath == "" {
		log.Fatal("--config is required and must point to a YAML file")
	}

	bootCfg, err := config.Load(*configPath)
	if err != nil {
		log.Fatalf("failed to load config %q: %v", *configPath, err)
	}
	var provider *spiffe.Provider
	if bootCfg.SPIFFE != nil {
		mirrorFiles := true
		if bootCfg.SPIFFE.MirrorFiles != nil {
			mirrorFiles = *bootCfg.SPIFFE.MirrorFiles
		}
		provider, err = spiffe.NewProvider(context.Background(), spiffe.ProviderConfig{
			SocketPath:  bootCfg.SPIFFE.Socket,
			MirrorFiles: mirrorFiles,
			MirrorDir:   bootCfg.SPIFFE.MirrorDir,
		})
		if err != nil {
			log.Fatalf("spiffe provider: %v", err)
		}
		defer provider.Close()
	}

	buildPipelines := func() (*pipeline.Pipeline, *pipeline.Pipeline, *config.Config, error) {
		c, err := config.Load(*configPath)
		if err != nil {
			return nil, nil, nil, err
		}
		if c.Mode != "" && c.Mode != config.ModeProxySidecar {
			return nil, nil, nil, fmt.Errorf(
				"authbridge-cpex supports only mode=%q (got %q); use cmd/authbridge-envoy for envoy-sidecar mode",
				config.ModeProxySidecar, c.Mode)
		}
		c.Mode = config.ModeProxySidecar
		config.ApplyPreset(c)
		if err := config.Validate(c); err != nil {
			return nil, nil, nil, err
		}
		config.WarnEmptyPipelines(c, slog.Default())
		in, err := plugins.BuildWithSPIFFE(c.Pipeline.Inbound.Plugins, provider)
		if err != nil {
			return nil, nil, nil, fmt.Errorf("inbound: %w", err)
		}
		out, err := plugins.BuildWithSPIFFE(c.Pipeline.Outbound.Plugins, provider)
		if err != nil {
			return nil, nil, nil, fmt.Errorf("outbound: %w", err)
		}
		return in, out, c, nil
	}

	inboundPipeline, outboundPipeline, cfg, err := buildPipelines()
	if err != nil {
		log.Fatalf("initial pipeline build: %v", err)
	}

	initCtx, initCancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer initCancel()
	if err := inboundPipeline.Start(initCtx); err != nil {
		log.Fatalf("inbound pipeline Start: %v", err)
	}
	if err := outboundPipeline.Start(initCtx); err != nil {
		log.Fatalf("outbound pipeline Start: %v", err)
	}

	inboundH := pipeline.NewHolder(inboundPipeline)
	outboundH := pipeline.NewHolder(outboundPipeline)

	ctx, cancelCtx := context.WithCancel(context.Background())
	defer cancelCtx()
	rld := reloader.New(*configPath, inboundH, outboundH, buildPipelines, cfg)
	if err := rld.Start(ctx); err != nil {
		log.Fatalf("reloader: %v", err)
	}

	var sessions *session.Store
	if cfg.Session.SessionEnabled() {
		ttl := 30 * time.Minute
		if cfg.Session.TTL != "" {
			if d, err := time.ParseDuration(cfg.Session.TTL); err == nil {
				ttl = d
			} else {
				slog.Warn("invalid session.ttl, using default", "value", cfg.Session.TTL, "error", err)
			}
		}
		maxEvents := 100
		if cfg.Session.MaxEvents > 0 {
			maxEvents = cfg.Session.MaxEvents
		}
		maxSessions := 100
		if cfg.Session.MaxSessions > 0 {
			maxSessions = cfg.Session.MaxSessions
		}
		sessions = session.New(ttl, maxEvents, maxSessions)
		slog.Info("session tracking enabled", "ttl", ttl, "maxEvents", maxEvents, "maxSessions", maxSessions)
	} else {
		slog.Info("session tracking disabled")
	}

	var httpServers []*http.Server

	var (
		rpMTLS      *reverseproxy.MTLSOptions
		fpMTLS      *forwardproxy.MTLSOptions
		mtlsMetrics *authtls.Metrics
	)
	if cfg.MTLS != nil {
		if provider == nil {
			log.Fatal("mtls requires the spiffe block to be configured")
		}
		strict := cfg.MTLS.ResolvedMode() == config.MTLSModeStrict
		src := provider.X509Source()
		mtlsMetrics = authtls.NewMetrics()
		rpMTLS = &reverseproxy.MTLSOptions{Source: src, Strict: strict, Metrics: mtlsMetrics}
		if strict {
			fpMTLS = &forwardproxy.MTLSOptions{Source: src, Metrics: mtlsMetrics}
		}
		slog.Info("mTLS enabled", "mode", cfg.MTLS.ResolvedMode())
	} else {
		slog.Info("mTLS disabled (no mtls block in config)")
	}

	rpSrv, err := reverseproxy.NewServer(inboundH, sessions, cfg.Listener.ReverseProxyBackend, rpMTLS)
	if err != nil {
		log.Fatalf("creating reverse proxy: %v", err)
	}
	fpSrv, err := forwardproxy.NewServer(outboundH, sessions, fpMTLS)
	if err != nil {
		log.Fatalf("creating forward proxy: %v", err)
	}
	sharedStore := shared.New()
	defer sharedStore.Close()
	rpSrv.Shared = sharedStore
	fpSrv.Shared = sharedStore
	rpHTTP, err := runtimeutil.StartReverseProxyServer("reverse-proxy", rpSrv, cfg.Listener.ReverseProxyAddr)
	if err != nil {
		log.Fatalf("reverse-proxy listen: %v", err)
	}
	httpServers = append(httpServers, rpHTTP)
	fpHTTP, err := runtimeutil.StartHTTPServer("forward-proxy", fpSrv.Handler(), cfg.Listener.ForwardProxyAddr)
	if err != nil {
		log.Fatalf("forward-proxy listen: %v", err)
	}
	httpServers = append(httpServers, fpHTTP)
	_ = mtlsMetrics

	statsProvider := func() *auth.Stats {
		sources := plugins.CollectStats(inboundH.Load())
		sources = append(sources, plugins.CollectStats(outboundH.Load())...)
		return auth.MergeStats(sources...)
	}
	statSrv, statErr := runtimeutil.StartStatServer(cfg, rld.ConfigProvider(), statsProvider, rld.Handler(), cfg.Stats.StatsAddress)
	if statErr != nil {
		log.Fatalf("stat server listen: %v", statErr)
	}

	plugins.WarmCatalog()

	var sessionAPISrv *sessionapi.Server
	if cfg.Listener.SessionAPIAddr != "" && sessions != nil {
		sessionAPISrv = sessionapi.New(
			cfg.Listener.SessionAPIAddr,
			sessions,
			sessionapi.WithPipelines(inboundH, outboundH),
			sessionapi.WithCatalog(sessionapi.PluginsCatalog),
		)
		go func() {
			slog.Warn("session API listening — UNAUTHENTICATED; contains raw user content; never expose via ingress",
				"addr", cfg.Listener.SessionAPIAddr)
			if err := sessionAPISrv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
				log.Fatalf("session API: %v", err)
			}
		}()
	}

	slog.Info("authbridge-cpex starting", "mode", cfg.Mode, "logLevel", runtimeutil.LogLevel().String())

	healthSrv, healthErr := runtimeutil.StartHealthServer(inboundH, outboundH, ":9091")
	if healthErr != nil {
		log.Fatalf("health server listen: %v", healthErr)
	}

	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGTERM, syscall.SIGINT)
	sig := <-sigCh
	slog.Info("shutting down", "signal", sig)

	shutdownCtx, shutdownCancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer shutdownCancel()

	for _, srv := range httpServers {
		srv.Shutdown(shutdownCtx)
	}
	statSrv.Shutdown(shutdownCtx)
	healthSrv.Shutdown(shutdownCtx)
	if sessionAPISrv != nil {
		sessionAPISrv.Shutdown(shutdownCtx)
	}

	outboundPipeline.Stop(shutdownCtx)
	inboundPipeline.Stop(shutdownCtx)

	if sessions != nil {
		sessions.Close()
	}
}
