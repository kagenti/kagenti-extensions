// Package lineage provides the lineage-telemetry authbridge plugin.
// It emits one OpenTelemetry span per request hop, linking spans via the
// W3C traceparent header so the full agent execution graph is reconstructed
// in the OTel collector / backend.
//
// Hop classification:
//   - Inbound  → principal_to_agent
//   - Outbound + A2A parser       → agent_to_agent
//   - Outbound + MCP parser       → agent_to_tool
//   - Outbound + Inference parser → agent_to_llm
//   - Outbound + no parser        → agent_to_service
//
// The plugin implements Finisher so spans are ended even when an earlier
// plugin denies the request; pctx.Outcome() is available at that point.
package lineage

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"strings"
	"sync/atomic"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracegrpc"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	semconv "go.opentelemetry.io/otel/semconv/v1.26.0"
	"go.opentelemetry.io/otel/trace"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"

	"github.com/kagenti/kagenti-extensions/authbridge/authlib/pipeline"
	"github.com/kagenti/kagenti-extensions/authbridge/authlib/plugins"
)

const pluginName = "lineage-telemetry"

func init() {
	plugins.RegisterPlugin(pluginName, func() pipeline.Plugin { return NewLineageTelemetry() })
}

// hopState carries per-request lineage context from OnRequest to OnFinish.
type hopState struct {
	span trace.Span
	info hopInfo
}

// LineageTelemetry emits OTel spans for each request hop observed by authbridge.
type LineageTelemetry struct {
	cfg        Config
	tp         *sdktrace.TracerProvider
	tracer     trace.Tracer
	ready      atomic.Bool
	propagator propagation.TextMapPropagator
	selfID     string // agent's own client ID for outbound caller attribution
}

// NewLineageTelemetry constructs an unconfigured plugin. Configure + Init must
// run before it serves traffic (guarded by Ready()).
func NewLineageTelemetry() *LineageTelemetry {
	return &LineageTelemetry{
		propagator: propagation.TraceContext{},
	}
}

func (p *LineageTelemetry) Name() string { return pluginName }

func (p *LineageTelemetry) Capabilities() pipeline.PluginCapabilities {
	return pipeline.PluginCapabilities{
		// Soft ordering: if parsers are present they must run first so
		// Extensions are populated when we read them. Missing parsers are
		// allowed — we fall back to HopAgentToService / HopPrincipalToAgent.
		After: []string{"a2a-parser", "mcp-parser", "inference-parser", "jwt-validation"},
	}
}

func (p *LineageTelemetry) Configure(raw json.RawMessage) error {
	cfg, err := decodeConfig(raw)
	if err != nil {
		return err
	}
	p.cfg = cfg
	return nil
}

func (p *LineageTelemetry) Init(ctx context.Context) error {
	endpoint := p.cfg.OTelEndpoint
	conn, err := grpc.NewClient(endpoint,
		grpc.WithTransportCredentials(insecure.NewCredentials()),
	)
	if err != nil {
		return fmt.Errorf("lineage-telemetry: gRPC dial %s: %w", endpoint, err)
	}

	exporter, err := otlptracegrpc.New(ctx,
		otlptracegrpc.WithGRPCConn(conn),
	)
	if err != nil {
		return fmt.Errorf("lineage-telemetry: OTLP exporter: %w", err)
	}

	res, err := resource.New(ctx,
		resource.WithAttributes(
			semconv.ServiceNameKey.String("authbridge"),
			attribute.String("authbridge.component", pluginName),
		),
	)
	if err != nil {
		slog.Warn("lineage-telemetry: resource detection failed, using default", "error", err)
		res = resource.Default()
	}

	p.tp = sdktrace.NewTracerProvider(
		sdktrace.WithBatcher(exporter),
		sdktrace.WithResource(res),
	)
	p.tracer = p.tp.Tracer("authbridge/" + pluginName)

	// Resolve self identity for outbound caller attribution.
	if p.cfg.SelfID != "" {
		p.selfID = p.cfg.SelfID
	} else if p.cfg.SelfIDFile != "" {
		if raw, err := os.ReadFile(p.cfg.SelfIDFile); err == nil {
			p.selfID = strings.TrimSpace(string(raw))
		} else {
			slog.Warn("lineage-telemetry: could not read self_id_file; outbound caller will be empty",
				"path", p.cfg.SelfIDFile, "error", err)
		}
	}

	p.ready.Store(true)
	slog.Info("lineage-telemetry: initialized", "endpoint", endpoint, "self_id", p.selfID)
	return nil
}

func (p *LineageTelemetry) Shutdown(ctx context.Context) error {
	if p.tp == nil {
		return nil
	}
	return p.tp.Shutdown(ctx)
}

func (p *LineageTelemetry) Ready() bool { return p.ready.Load() }

func (p *LineageTelemetry) OnRequest(ctx context.Context, pctx *pipeline.Context) pipeline.Action {
	if !p.ready.Load() {
		pctx.Skip("not_ready")
		return pipeline.Action{Type: pipeline.Continue}
	}

	// Skip infrastructure paths (health checks, agent-card discovery, etc.)
	for _, prefix := range p.cfg.BypassPaths {
		if strings.HasPrefix(pctx.Path, prefix) {
			pctx.Skip("bypass_path")
			return pipeline.Action{Type: pipeline.Continue}
		}
	}

	// Skip infrastructure outbound targets (OTel exporters, metrics scrapers, etc.)
	for _, substr := range p.cfg.BypassHosts {
		if strings.Contains(pctx.Host, substr) {
			pctx.Skip("bypass_host")
			return pipeline.Action{Type: pipeline.Continue}
		}
	}

	// Extract remote trace context from incoming W3C traceparent header.
	carrier := make(propagation.MapCarrier, len(pctx.Headers))
	for k, vs := range pctx.Headers {
		if len(vs) > 0 {
			carrier[k] = vs[0]
		}
	}
	remoteCtx := p.propagator.Extract(ctx, carrier)

	info := determineHop(pctx)
	callerID := callerIdentity(pctx, p.selfID)
	targetID := pctx.Host

	_, span := p.tracer.Start(remoteCtx, "lineage.hop",
		trace.WithSpanKind(trace.SpanKindServer),
		trace.WithAttributes(
			attribute.String("lineage.hop.kind", string(info.Kind)),
			attribute.String("lineage.direction", pctx.Direction.String()),
			attribute.String("lineage.protocol", info.Protocol),
			attribute.String("lineage.caller.id", callerID),
			attribute.String("lineage.target.id", targetID),
		),
	)

	pipeline.SetState(pctx, pluginName, &hopState{span: span, info: info})
	pctx.Observe("recorded_hop")
	return pipeline.Action{Type: pipeline.Continue}
}

func (p *LineageTelemetry) OnResponse(_ context.Context, _ *pipeline.Context) pipeline.Action {
	return pipeline.Action{Type: pipeline.Continue}
}

// OnFinish ends the span with outcome attributes. Runs under a recover so a
// nil span or unexpected state never crashes the pipeline.
func (p *LineageTelemetry) OnFinish(_ context.Context, pctx *pipeline.Context) {
	defer func() {
		if r := recover(); r != nil {
			slog.Warn("lineage-telemetry: OnFinish panic recovered", "recover", r)
		}
	}()

	state := pipeline.GetState[hopState](pctx, pluginName)
	if state == nil || state.span == nil {
		return
	}

	outcome := pctx.Outcome()
	if outcome == nil {
		state.span.End()
		return
	}

	attrs := []attribute.KeyValue{
		attribute.String("lineage.outcome", string(outcome.FinalAction)),
		attribute.Int("http.status_code", outcome.StatusCode),
	}
	if outcome.DenyingPlugin != "" {
		attrs = append(attrs, attribute.String("lineage.denying_plugin", outcome.DenyingPlugin))
	}

	ext := pctx.Extensions
	if ext.A2A != nil {
		attrs = append(attrs,
			attribute.String("a2a.method", ext.A2A.Method),
			attribute.String("a2a.session_id", ext.A2A.SessionID),
		)
	}
	if ext.MCP != nil {
		attrs = append(attrs,
			attribute.String("mcp.method", ext.MCP.Method),
		)
	}
	if ext.Inference != nil {
		attrs = append(attrs,
			attribute.String("inference.model", ext.Inference.Model),
		)
	}

	state.span.SetAttributes(attrs...)
	state.span.End()
}

// callerIdentity extracts a stable caller identifier from the request context.
// Prefers Subject (end-user ID) over ClientID (service account / client name).
// Falls back to selfID (the agent's own ID) for outbound requests where no
// inbound identity has been established.
func callerIdentity(pctx *pipeline.Context, selfID string) string {
	if pctx.Identity != nil {
		if s := pctx.Identity.Subject(); s != "" {
			return s
		}
		// Subject is absent (service-account grant) — fall back to client ID.
		if c := pctx.Identity.ClientID(); c != "" {
			return c
		}
	}
	if pctx.Agent != nil && pctx.Agent.ClientID != "" {
		return pctx.Agent.ClientID
	}
	// Outbound requests carry no inbound identity; use the agent's own ID.
	return selfID
}

// Compile-time interface assertions.
var (
	_ pipeline.Plugin       = (*LineageTelemetry)(nil)
	_ pipeline.Configurable = (*LineageTelemetry)(nil)
	_ pipeline.Initializer  = (*LineageTelemetry)(nil)
	_ pipeline.Shutdowner   = (*LineageTelemetry)(nil)
	_ pipeline.Finisher     = (*LineageTelemetry)(nil)
	_ pipeline.Readier      = (*LineageTelemetry)(nil)
)
