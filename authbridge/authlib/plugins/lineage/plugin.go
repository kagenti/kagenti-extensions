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
	"time"

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

// inboundSpans is process-wide so the forward-proxy instance (outbound
// pipeline) can look up spans written by the reverse-proxy instance
// (inbound pipeline). Both instances run in the same authbridge process
// but are created separately by the plugin factory.
var inboundSpans sync.Map // map[traceID string]trace.SpanContext

// agentCurrentInbound maps selfID → SpanContext for the currently-active
// inbound request on that agent. Used as a fallback when an outbound call
// (e.g. from Google ADK) carries a stale trace ID that does not match any
// entry in inboundSpans — the span is re-parented under the agent's current
// inbound request regardless of the trace ID in the outbound headers.
var agentCurrentInbound sync.Map // map[selfID string]trace.SpanContext

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
	selfID string // agent's own client ID for outbound caller attribution
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
	// IsPrincipal: reclassify outbound A2A hops so orchestrator agents
	// (e.g. trip-demo) appear as chain initiators rather than peer agents.
	if p.cfg.IsPrincipal && info.Kind == HopAgentToAgent && pctx.Direction == pipeline.Outbound {
		info.Kind = HopPrincipalToAgent
	}

	// For outbound hops: re-parent directly under the inbound authbridge span
	// (same trace_id) rather than under the Python httpx span. The OTel
	// collector's filter/phoenix drops httpx spans (http.method != nil &&
	// openinference.span.kind == nil), leaving outbound authbridge spans
	// visually orphaned in Phoenix. All outbound spans become direct children
	// of the inbound span, giving a flat one-level tree in Phoenix.
	if pctx.Direction == pipeline.Outbound {
		linked := false
		remoteSpanCtx := trace.SpanContextFromContext(remoteCtx)
		if remoteSpanCtx.IsValid() {
			traceID := remoteSpanCtx.TraceID().String()
			if val, ok := inboundSpans.Load(traceID); ok {
				remoteCtx = trace.ContextWithRemoteSpanContext(ctx, val.(trace.SpanContext))
				linked = true
			}
		}
		// Fallback: re-parent under the agent's current inbound span.
		// Handles two cases:
		//  1. Python injects no traceparent (no HTTPXClientInstrumentor) so
		//     remoteSpanCtx is invalid and the block above is skipped entirely.
		//  2. The outbound call carries a stale/different trace ID (e.g. Google
		//     ADK MCPToolset sessions that persist across requests).
		if !linked && p.selfID != "" {
			if val, ok := agentCurrentInbound.Load(p.selfID); ok {
				remoteCtx = trace.ContextWithRemoteSpanContext(ctx, val.(trace.SpanContext))
			}
		}
	}

	// Normalize IDs to short service names; keep raw addresses separately.
	sourceID := serviceLabel(sourceIdentity(pctx, p.selfID, pctx.Direction == pipeline.Inbound))
	rawTarget := pctx.Host
	targetID := shortHostname(rawTarget)

	// Anonymous inbound: principal_to_agent with no identity. The span is still
	// created (trace propagation must happen), but lineage.hop.kind is omitted so
	// filter/lineage drops it — no "10 → agent" noise in Execution Flow.
	// anonymousInbound only applies to ACTUAL inbound hops (reverse proxy),
	// not to outbound hops reclassified as principal_to_agent via is_principal.
	anonymousInbound := info.Kind == HopPrincipalToAgent &&
		pctx.Direction == pipeline.Inbound &&
		pctx.Identity == nil && pctx.Agent == nil

	spanKind, oiKind := hopSpanKinds(info)
	spanAttrs := []attribute.KeyValue{
		attribute.String("lineage.direction", pctx.Direction.String()),
		attribute.String("lineage.protocol", info.Protocol),
		attribute.String("lineage.source.id", sourceID),
		attribute.String("lineage.target.id", targetID),
		attribute.String("openinference.span.kind", oiKind),
		attribute.Bool("authbridge.proxy", true),
		// trust.* attrs are the canonical keys consumed by the lineage service
		// transformer. Set them directly so the lineage pipeline works without
		// an OTel transform fallback for authbridge-originated spans.
		attribute.String("trust.source_id", sourceID),
		attribute.String("trust.target_id", targetID),
	}
	// lineage.hop.kind and trust.hop_kind are what the filter/lineage OTel
	// processor uses to route spans to the lineage service. Omit them for
	// anonymous inbound hops (no identity available) so those hops are
	// silently dropped by filter/lineage and never appear in Execution Flow,
	// while the span itself is still emitted and propagates trace context.
	if !anonymousInbound {
		spanAttrs = append(spanAttrs,
			attribute.String("lineage.hop.kind", string(info.Kind)),
			attribute.String("trust.hop_kind", string(info.Kind)),
		)
	}
	if pctx.RemoteAddr != "" {
		spanAttrs = append(spanAttrs, attribute.String("lineage.source.addr", pctx.RemoteAddr))
	}
	if rawTarget != "" {
		spanAttrs = append(spanAttrs, attribute.String("lineage.target.addr", rawTarget))
	}
	// enduser.id carries the human-readable username (preferred_username claim)
	// for inbound hops initiated by a human user. The lineage service reads this
	// to populate the `username` field on runs, distinguishing user-initiated
	// runs from service-to-service calls.
	if pctx.Identity != nil {
		if u := pctx.Identity.Username(); u != "" {
			spanAttrs = append(spanAttrs, attribute.String("enduser.id", u))
		}
		// trust.principal_id: the human or service identity that initiated this
		// request chain. Set from the authenticated subject on inbound hops.
		if s := pctx.Identity.Subject(); s != "" {
			spanAttrs = append(spanAttrs, attribute.String("trust.principal_id", s))
		}
	}

	// For principal_to_agent hops with no authenticated identity (e.g. bypass_paths
	// on demo clusters), we still create the span so that:
	//   1. The authbridge injects a traceparent into the forwarded request headers,
	//      keeping the Python agent's spans in the same trace as the callers.
	//      Skipping the span entirely breaks this: Python starts a fresh trace.
	//   2. inboundSpans / agentCurrentInbound are registered for outbound re-parenting.
	// However, we suppress the hop from the lineage service by NOT setting
	// lineage.hop.kind — the filter/lineage OTel processor drops spans with
	// lineage.hop.kind == nil, so "10 → agent" noise never reaches Execution Flow.
	// On auth-enabled clusters pctx.Identity is set, lineage.hop.kind is included,
	// and the hop appears correctly in Execution Flow.

	var method string
	if pctx.Extensions.MCP != nil {
		method = pctx.Extensions.MCP.Method
		// For tool calls, use the tool name (e.g. "search_destinations") as
		// the span label rather than the generic "tools/call" MCP method name.
		if method == "tools/call" && pctx.Extensions.MCP.Params != nil {
			if name, ok := pctx.Extensions.MCP.Params["name"].(string); ok && name != "" {
				method = name
			}
		}
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
	// Also store by agent identity so outbound calls with mismatched trace IDs
	// (e.g. from Google ADK MCPToolset sessions that carry a stale context) can
	// still be re-parented under the current inbound request.
	if info.Kind == HopPrincipalToAgent {
		inboundSpans.Store(span.SpanContext().TraceID().String(), span.SpanContext())
		if p.selfID != "" {
			agentCurrentInbound.Store(p.selfID, span.SpanContext())
		}
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

	// Defer inbound span removal: the SSE connection from the client may be
	// cut by a proxy/ztunnel idle-timeout before the agent finishes processing.
	// Deleting immediately would break parent links for outbound spans emitted
	// after the client connection drops but while run_turn is still running.
	// A 5-minute TTL is well beyond any realistic agent turn duration.
	if state.info.Kind == HopPrincipalToAgent {
		traceID := state.span.SpanContext().TraceID().String()
		selfID := p.selfID
		time.AfterFunc(5*time.Minute, func() {
			inboundSpans.Delete(traceID)
			if selfID != "" {
				agentCurrentInbound.Delete(selfID)
			}
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

	if p.cfg.CaptureIO {
		if v := ioInputValue(pctx); v != "" {
			attrs = append(attrs, attribute.String("input.value", v))
		}
		if v := ioOutputValue(pctx); v != "" {
			attrs = append(attrs, attribute.String("output.value", v))
		}
	}

	if outcome.DenyingPlugin != "" || outcome.StatusCode >= 400 {
		state.span.SetStatus(codes.Error, outcome.DenyingPlugin)
	} else {
		state.span.SetStatus(codes.Ok, "")
	}
	state.span.SetAttributes(attrs...)
	state.span.End()
}

// sourceIdentity extracts a stable source identifier from the request context.
// Prefers Subject (end-user ID) over ClientID (service account / client name).
//
// For outbound hops: falls back to selfID (the agent's own ID) when no
// inbound identity has been established.
//
// For inbound hops (isInbound=true): falls back to the caller's remote address
// rather than selfID, which would produce a confusing self-edge when JWT
// validation is bypassed (e.g. no-auth demo clusters).
func sourceIdentity(pctx *pipeline.Context, selfID string, isInbound bool) string {
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
	if isInbound {
		// No JWT on an inbound request (auth bypassed): use the caller's
		// remote address so the source ≠ target.  shortHostname strips the
		// port, leaving a pod IP that is at least unambiguous.
		if pctx.RemoteAddr != "" {
			return shortHostname(pctx.RemoteAddr)
		}
		return "unknown"
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
		// A2A method is always "message/stream" — omit the suffix.
		return svc + ".a2a"
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

// shortHostname reduces a Kubernetes FQDN (with optional port) to just the
// first DNS label, which is the short service name.
// "weather-service.team1.svc.cluster.local:8080" → "weather-service"
// "weather-tool-mcp.team1.svc.cluster.local:8000" → "weather-tool-mcp"
// "plain-host" → "plain-host"
func shortHostname(host string) string {
	if host == "" {
		return host
	}
	// Strip port.
	h := host
	if i := strings.LastIndex(h, ":"); i >= 0 {
		h = h[:i]
	}
	// Take the first DNS label.
	if i := strings.Index(h, "."); i >= 0 {
		return h[:i]
	}
	return h
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

// ioInputValue returns the OpenInference input.value for a span: the parsed
// request content for the hop's protocol, or "" if nothing meaningful is available.
func ioInputValue(pctx *pipeline.Context) string {
	ext := pctx.Extensions
	switch {
	case ext.A2A != nil && len(ext.A2A.Parts) > 0:
		// Collect all text parts; fall back to JSON if non-text parts present.
		var texts []string
		for _, p := range ext.A2A.Parts {
			if p.Content != "" {
				texts = append(texts, p.Content)
			}
		}
		if len(texts) > 0 {
			return strings.Join(texts, "\n")
		}
		if b, err := json.Marshal(ext.A2A.Parts); err == nil {
			return string(b)
		}
	case ext.Inference != nil && len(ext.Inference.Messages) > 0:
		if b, err := json.Marshal(ext.Inference.Messages); err == nil {
			return string(b)
		}
	case ext.MCP != nil && ext.MCP.Params != nil:
		// For tools/call, surface just the arguments (the semantically
		// meaningful part) rather than the full {"name":…,"arguments":…} wrapper.
		if ext.MCP.Method == "tools/call" {
			if args, ok := ext.MCP.Params["arguments"]; ok {
				if b, err := json.Marshal(args); err == nil {
					return string(b)
				}
			}
		}
		if b, err := json.Marshal(ext.MCP.Params); err == nil {
			return string(b)
		}
	}
	return ""
}

// isA2AProtocolEvent returns true when s is a JSON object carrying an A2A
// transport-level "kind" field (status-update, task-status-update, etc.)
// rather than actual content. Used to avoid surfacing protocol metadata
// as output.value when the a2a-parser captures a protocol event as the
// artifact instead of the real agent response text.
func isA2AProtocolEvent(s string) bool {
	var obj map[string]json.RawMessage
	if json.Unmarshal([]byte(s), &obj) != nil {
		return false
	}
	var kind string
	if raw, ok := obj["kind"]; ok {
		_ = json.Unmarshal(raw, &kind)
	}
	return strings.Contains(kind, "status") || strings.Contains(kind, "artifact-update") ||
		strings.Contains(kind, "Status") || kind == "working" || kind == "canceled"
}

// ioOutputValue returns the OpenInference output.value for a span: the parsed
// response content for the hop's protocol, or "" if nothing is available.
func ioOutputValue(pctx *pipeline.Context) string {
	ext := pctx.Extensions
	switch {
	case ext.A2A != nil && ext.A2A.Artifact != "" && !isA2AProtocolEvent(ext.A2A.Artifact):
		return ext.A2A.Artifact
	case ext.A2A != nil && ext.A2A.ErrorMessage != "":
		return ext.A2A.ErrorMessage
	case ext.Inference != nil && ext.Inference.Completion != "":
		return ext.Inference.Completion
	case ext.Inference != nil && len(ext.Inference.ToolCalls) > 0:
		if b, err := json.Marshal(ext.Inference.ToolCalls); err == nil {
			return string(b)
		}
	case ext.MCP != nil && ext.MCP.Result != nil:
		// For tools/call results, extract the text content from the MCP
		// content array rather than returning the full {"content":[…],"_meta":…}
		// envelope, so the output matches what Phoenix shows for the tool span.
		if ext.MCP.Method == "tools/call" {
			if content, ok := ext.MCP.Result["content"]; ok {
				if items, ok := content.([]any); ok {
					var texts []string
					for _, item := range items {
						if m, ok := item.(map[string]any); ok {
							if m["type"] == "text" {
								if t, ok := m["text"].(string); ok && t != "" {
									texts = append(texts, t)
								}
							}
						}
					}
					if len(texts) > 0 {
						return strings.Join(texts, "\n")
					}
				}
			}
		}
		if b, err := json.Marshal(ext.MCP.Result); err == nil {
			return string(b)
		}
	case ext.MCP != nil && ext.MCP.Err != nil:
		if b, err := json.Marshal(ext.MCP.Err); err == nil {
			return string(b)
		}
	}
	return ""
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
