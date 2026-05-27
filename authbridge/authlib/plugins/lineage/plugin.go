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
	"sync"
	"sync/atomic"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
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

	// inboundSpans tracks the live inbound span context for each trace, keyed
	// by trace ID (hex string). Outbound hops use it to parent themselves
	// directly under the inbound authbridge span rather than under the
	// intermediate httpx spans from the Python app, which are filtered out
	// by the OTel collector's filter/phoenix processor before reaching Phoenix.
	inboundSpans sync.Map // map[traceID string]trace.SpanContext

	// serviceSpans tracks CHAIN (agent_to_service) span contexts keyed by
	// "traceID:host". TOOL/LLM/A2A outbound hops to the same host are
	// re-parented under the CHAIN span so the SSE setup appears as the
	// parent of subsequent MCP method calls in Phoenix.
	serviceSpans sync.Map // map["traceID:host" string]trace.SpanContext
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
	// HeaderCarrier wraps http.Header and uses case-insensitive Get/Keys so
	// canonical-form keys ("Traceparent") match the propagator's lowercase
	// lookups. The previous MapCarrier build did an exact-match lookup and
	// always missed, causing every hop to start a new root trace.
	remoteCtx := p.propagator.Extract(ctx, propagation.HeaderCarrier(pctx.Headers))

	info := determineHop(pctx)

	// For outbound hops: re-parent to preserve the logical call hierarchy.
	// CHAIN hops (SSE setup / unknown service): parent under inbound span.
	// TOOL/LLM/A2A hops: parent under a CHAIN span for the same host when
	// one exists (MCP over streamable HTTP opens an SSE GET before POSTing
	// methods), falling back to the inbound span. This makes MCP method
	// calls appear as children of the SSE connection span in Phoenix rather
	// than as siblings of it.
	if pctx.Direction == pipeline.Outbound {
		remoteSpanCtx := trace.SpanContextFromContext(remoteCtx)
		if remoteSpanCtx.IsValid() {
			traceID := remoteSpanCtx.TraceID().String()
			if info.Kind == HopAgentToService {
				// CHAIN: re-parent directly under inbound span.
				if val, ok := p.inboundSpans.Load(traceID); ok {
					remoteCtx = trace.ContextWithRemoteSpanContext(ctx, val.(trace.SpanContext))
				}
			} else {
				// TOOL/LLM/A2A: prefer a CHAIN span for the same host.
				key := traceID + ":" + pctx.Host
				if val, ok := p.serviceSpans.Load(key); ok {
					remoteCtx = trace.ContextWithRemoteSpanContext(ctx, val.(trace.SpanContext))
				} else if val, ok := p.inboundSpans.Load(traceID); ok {
					remoteCtx = trace.ContextWithRemoteSpanContext(ctx, val.(trace.SpanContext))
				}
			}
		}
	}

	callerID := callerIdentity(pctx, p.selfID)
	targetID := pctx.Host

	spanKind, oiKind := hopSpanKinds(info)
	spanAttrs := []attribute.KeyValue{
		attribute.String("lineage.hop.kind", string(info.Kind)),
		attribute.String("lineage.direction", pctx.Direction.String()),
		attribute.String("lineage.protocol", info.Protocol),
		attribute.String("lineage.caller.id", callerID),
		attribute.String("lineage.target.id", targetID),
		attribute.String("openinference.span.kind", oiKind),
	}
	// enduser.id carries the human-readable username (preferred_username claim)
	// for inbound hops initiated by a human user. The lineage service reads this
	// to populate the `username` field on runs, distinguishing user-initiated
	// runs from service-to-service calls.
	if pctx.Identity != nil {
		if u := pctx.Identity.Username(); u != "" {
			spanAttrs = append(spanAttrs, attribute.String("enduser.id", u))
		}
	}

	var method string
	if pctx.Extensions.MCP != nil {
		method = pctx.Extensions.MCP.Method
	} else if pctx.Extensions.A2A != nil {
		method = pctx.Extensions.A2A.Method
	}
	spanName := hopSpanName(p.selfID, info, method)
	spanCtx, span := p.tracer.Start(remoteCtx, spanName,
		trace.WithSpanKind(spanKind),
		trace.WithAttributes(spanAttrs...),
	)

	// For inbound hops: register this span so outbound hops in the same trace
	// can parent themselves directly under it, bypassing the filtered httpx spans.
	if info.Kind == HopPrincipalToAgent {
		p.inboundSpans.Store(span.SpanContext().TraceID().String(), span.SpanContext())
	}
	// For CHAIN hops: register by "traceID:host" so subsequent TOOL/LLM/A2A
	// hops to the same host are parented under this span.
	if info.Kind == HopAgentToService {
		key := span.SpanContext().TraceID().String() + ":" + pctx.Host
		p.serviceSpans.Store(key, span.SpanContext())
	}

	// Propagate the new span context into the forwarded request so that:
	//   - inbound:  the backend app creates its spans as children of this span,
	//               and its outbound calls carry this traceparent forward.
	//   - outbound: downstream agents/tools see this authbridge hop as their
	//               parent, threading A2A and tool calls into the same trace.
	p.propagator.Inject(spanCtx, propagation.HeaderCarrier(pctx.Headers))

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

	// Remove the inbound span entry so the map doesn't grow unboundedly.
	// Also purge all service (CHAIN) spans for the trace.
	if state.info.Kind == HopPrincipalToAgent {
		traceID := state.span.SpanContext().TraceID().String()
		p.inboundSpans.Delete(traceID)
		p.serviceSpans.Range(func(k, _ any) bool {
			if strings.HasPrefix(k.(string), traceID+":") {
				p.serviceSpans.Delete(k)
			}
			return true
		})
	}

	outcome := pctx.Outcome()
	if outcome == nil {
		state.span.SetStatus(codes.Ok, "")
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

	if outcome.DenyingPlugin != "" || outcome.StatusCode >= 400 {
		state.span.SetStatus(codes.Error, outcome.DenyingPlugin)
	} else {
		state.span.SetStatus(codes.Ok, "")
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

// hopSpanName returns a human-readable OTel span name for the hop.
// Format: "<service>.<protocol> [method]" where service is the last path
// segment of the SPIFFE ID (e.g. "weather-service" from
// "spiffe://localtest.me/ns/team1/sa/weather-service"), protocol is
// "inbound", "mcp", "llm", "a2a", or "outbound", and method is the
// optional protocol-level method name (e.g. "tools/call", "initialize").
func hopSpanName(selfID string, info hopInfo, method string) string {
	svc := serviceLabel(selfID)
	var base string
	switch info.Kind {
	case HopPrincipalToAgent:
		return svc + ".inbound"
	case HopAgentToTool:
		base = svc + ".mcp"
	case HopAgentToLLM:
		base = svc + ".llm"
	case HopAgentToAgent:
		base = svc + ".a2a"
	case HopAgentToService:
		// Use protocol hint ("mcp", "http") for CHAIN hops so initialize
		// and other MCP setup calls are distinguishable from plain HTTP.
		base = svc + "." + info.Protocol
	default:
		return svc + ".outbound"
	}
	if method != "" {
		return base + " " + method
	}
	return base
}

// serviceLabel extracts the last path segment from a SPIFFE ID, or returns
// selfID as-is if it is not a SPIFFE URI. Falls back to "agent" if empty.
func serviceLabel(selfID string) string {
	if selfID == "" {
		return "agent"
	}
	// "spiffe://trust-domain/ns/team1/sa/weather-service" → "weather-service"
	parts := strings.Split(selfID, "/")
	for i := len(parts) - 1; i >= 0; i-- {
		if parts[i] != "" {
			return parts[i]
		}
	}
	return selfID
}

// hopSpanKinds returns the OTel SpanKind and the OpenInference span kind string
// for a hop. OTel SpanKind drives standard backends; openinference.span.kind
// drives Phoenix icons and kind-label filtering.
//
// OpenInference kind vocabulary:
//
//	AGENT   – LLM agent invocation (inbound or A2A outbound)
//	LLM     – direct LLM inference call
//	TOOL    – tool call (MCP)
//	CHAIN   – generic processing step (unknown outbound service)
func hopSpanKinds(info hopInfo) (trace.SpanKind, string) {
	switch info.Kind {
	case HopPrincipalToAgent:
		return trace.SpanKindServer, "AGENT"
	case HopAgentToTool:
		return trace.SpanKindClient, "TOOL"
	case HopAgentToLLM:
		return trace.SpanKindClient, "LLM"
	case HopAgentToAgent:
		return trace.SpanKindClient, "AGENT"
	default:
		return trace.SpanKindClient, "CHAIN"
	}
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
