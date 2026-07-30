// Package lineage provides the lineage-telemetry authbridge plugin.
//
// Two-span model (see docs/sidecar-wire-contract.md — the law this file
// implements). Each HTTP exchange through the sidecar produces TWO OTLP spans:
//
//   - a request span, emitted as soon as the request has been seen and
//     forwarded, carrying caller-side facts + input.value; and
//   - a response span, emitted at stream end (even when no response was
//     produced), carrying status/outcome facts + output.value.
//
// Both spans are ended immediately at emission — no span is held open across
// the wait. lineage.exchange.id (= the request span's own span id) is echoed
// on both so the consumer pairs them. The plugin emits FACTS ONLY (direction,
// protocol, endpoints, parsed payloads); all vocabulary — hop kinds, entity
// kinds, caller/callee — lives in the consumer's classify(). See the "removed
// vs today" migration map in the contract for the attrs this no longer emits.
//
// The plugin implements Finisher so the response span is emitted at stream
// end whatever the outcome — including denials that happen AFTER the request
// span was recorded (a response-phase deny, or a request-phase deny by a
// plugin ordered after this one); pctx.Outcome() is available at that point
// and maps to lineage.outcome=denied + lineage.denied_by. LIMITATION: this
// plugin orders itself after the gate plugins (Capabilities.After includes
// jwt-validation) and the pipeline short-circuits on a request-phase Reject,
// so an exchange denied by a gate BEFORE OnRequest ran emits NO spans at all —
// it is invisible to lineage. Moving lineage ahead of the gates (spans for
// denied traffic too) is a named follow-up, not current behavior.
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

// tracestateStampKey is the W3C tracestate member this sidecar stamps on the
// request it forwards to its own app: value = the inbound request span id
// (the exchange id). The app's propagate-only shim carries tracestate through
// its per-request causal chain (contextvars), so the member surfaces on
// exactly the outbound calls that inbound caused — the only wire fact that
// stays unambiguous under CONCURRENT same-trace inbound exchanges, where the
// trace-keyed map (one entry per trace) collapses. Outbound parent precedence:
// stamp > map > wire parent; the chosen source is recorded as the
// lineage.parent.source fact.
const tracestateStampKey = "kglin"

// inboundSpans is process-wide so the forward-proxy instance (outbound
// pipeline) can look up spans written by the reverse-proxy instance
// (inbound pipeline). Both instances run in the same authbridge process
// but are created separately by the plugin factory. Keyed by trace_id →
// this pod's inbound request span for that trace. Entries live 5 minutes
// past exchange finish (see OnFinish) to tolerate SSE-connection drops.
var inboundSpans sync.Map // map[traceID string]trace.SpanContext

func init() {
	plugins.RegisterPlugin(pluginName, func() pipeline.Plugin { return NewLineageTelemetry() })
}

// exchangeState carries what OnFinish needs to emit the response span as the
// twin of the request span emitted in OnRequest.
type exchangeState struct {
	// reqCtx is the (already-ended) request span's context — the parent of
	// the response span. An ended span's SpanContext is a valid parent.
	reqCtx trace.SpanContext
	// common holds the attributes shared by both spans (lineage.direction,
	// self.id, peer.*, protocol, exchange.id) — NOT lineage.role, which
	// differs per span. Computed once so both spans agree byte-for-byte.
	common   []attribute.KeyValue
	spanKind trace.SpanKind
	// spanName is the request span's name; the response span appends " response".
	spanName string
	// protocol is the request span's lineage.protocol fact; the response
	// span's output.value must be read through the SAME protocol's parser
	// (parsers are precedence-ordered, not mutually exclusive — mcp-parser
	// also matches any JSON-RPC body, including every a2a exchange).
	protocol string
	inbound  bool
	traceID  string
}

// LineageTelemetry emits OTel spans for each request hop observed by authbridge.
type LineageTelemetry struct {
	cfg        Config
	tp         *sdktrace.TracerProvider
	tracer     trace.Tracer
	ready      atomic.Bool
	propagator propagation.TextMapPropagator
	selfID     string // agent's own client ID for the lineage.self.id fact
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
		// Extensions are populated when we read them to pick the protocol
		// fact. Missing parsers are allowed — protocolOf falls back to http.
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

	// Resolve self identity for the lineage.self.id fact.
	if p.cfg.SelfID != "" {
		p.selfID = p.cfg.SelfID
	} else if p.cfg.SelfIDFile != "" {
		if raw, err := os.ReadFile(p.cfg.SelfIDFile); err == nil {
			p.selfID = strings.TrimSpace(string(raw))
		} else {
			slog.Warn("lineage-telemetry: could not read self_id_file; lineage.self.id will be empty",
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

	// Extract remote trace context from the incoming W3C traceparent header.
	// HeaderCarrier wraps http.Header and uses case-insensitive Get/Keys so
	// canonical-form keys ("Traceparent") match the propagator's lowercase
	// lookups.
	remoteCtx := p.propagator.Extract(ctx, propagation.HeaderCarrier(pctx.Headers))

	protocol := protocolOf(pctx)
	self := serviceLabel(p.selfID)
	spanKind := spanKindFor(pctx.Direction)
	spanName := requestSpanName(self, protocol, spanOp(pctx, protocol))

	// Facts shared by both spans (exchange.id is appended once the request
	// span exists, since it IS the request span id).
	base := baseAttrs(pctx, self, protocol)

	// Request-span attributes: role + shared facts + request-only facts.
	reqAttrs := make([]attribute.KeyValue, 0, len(base)+8)
	reqAttrs = append(reqAttrs, attribute.String("lineage.role", "request"))
	reqAttrs = append(reqAttrs, base...)
	reqAttrs = p.appendRequestFacts(reqAttrs, pctx, protocol)

	reqCtx, exchangeID := p.spliceParent(ctx, pctx, remoteCtx, spanName, spanKind, reqAttrs)

	common := make([]attribute.KeyValue, 0, len(base)+1)
	common = append(common, base...)
	common = append(common, attribute.String("lineage.exchange.id", exchangeID))

	pipeline.SetState(pctx, pluginName, &exchangeState{
		reqCtx:   reqCtx,
		common:   common,
		spanKind: spanKind,
		spanName: spanName,
		protocol: protocol,
		inbound:  pctx.Direction == pipeline.Inbound,
		traceID:  reqCtx.TraceID().String(),
	})
	pctx.Observe("recorded_request")
	return pipeline.Action{Type: pipeline.Continue}
}

// spliceParent is the mesh splice — the ONE place the trace-keyed map is read,
// written, and injected. It (3) selects the request span's parent, (4) emits
// and immediately ends the request span, (5) publishes it into inboundSpans on
// inbound, and (6) rewrites the forwarded traceparent to name it on outbound.
// Returns the request span's context and its span id (the exchange id).
//
// >>> OPTION-4 DELETION POINT <<<
// The whole splice — this function plus inboundSpans and the 5-minute TTL in
// OnFinish — is exactly what a pure read-only sidecar would drop. Deleting this
// function and its inbound-store / outbound-inject reverts to wire-parent-only
// propagation. See the splice design note before attempting that spike.
func (p *LineageTelemetry) spliceParent(
	ctx context.Context,
	pctx *pipeline.Context,
	remoteCtx context.Context,
	spanName string,
	spanKind trace.SpanKind,
	reqAttrs []attribute.KeyValue,
) (trace.SpanContext, string) {
	// (3) Parent: outbound → the tracestate stamp (exact per-inbound
	// attribution, survives same-trace concurrency), else this pod's inbound
	// span for the same trace_id (the map), else the wire parent. Inbound
	// always uses the wire parent.
	parent := remoteCtx
	parentSource := "wire"
	if pctx.Direction == pipeline.Outbound {
		if rsc := trace.SpanContextFromContext(remoteCtx); rsc.IsValid() {
			if psc, ok := stampedParent(rsc); ok {
				parent = trace.ContextWithRemoteSpanContext(ctx, psc)
				parentSource = "tracestate"
			} else if v, ok := inboundSpans.Load(rsc.TraceID().String()); ok {
				parent = trace.ContextWithRemoteSpanContext(ctx, v.(trace.SpanContext))
				parentSource = "map"
			}
		}
	}
	reqAttrs = append(reqAttrs, attribute.String("lineage.parent.source", parentSource))

	// (4) Emit the request span and end it immediately. exchange.id is the
	// span's own id, so it can only be set after Start; an ended span's
	// SpanContext remains a valid parent for the response span.
	_, span := p.tracer.Start(parent, spanName,
		trace.WithSpanKind(spanKind),
		trace.WithAttributes(reqAttrs...),
	)
	sc := span.SpanContext()
	exchangeID := sc.SpanID().String()
	span.SetAttributes(attribute.String("lineage.exchange.id", exchangeID))
	span.End()

	// (5) Inbound → publish so this pod's outbound spans in the same trace
	// can splice themselves directly under it, and STAMP the forwarded
	// request's tracestate with this exchange id so the app couriers exact
	// per-inbound attribution back to the outbound side (see
	// tracestateStampKey). The stamp requires a valid wire traceparent —
	// without one the app's shim starts a fresh root trace and drops the
	// tracestate anyway. The listener is responsible for propagating this
	// header mutation to the app (ext_proc emits a SetHeaders diff).
	if pctx.Direction == pipeline.Inbound {
		inboundSpans.Store(sc.TraceID().String(), sc)
		if rsc := trace.SpanContextFromContext(remoteCtx); rsc.IsValid() {
			if ts, err := rsc.TraceState().Insert(tracestateStampKey, sc.SpanID().String()); err == nil {
				pctx.Headers.Set("tracestate", ts.String())
			}
		}
	}

	// (6) Outbound → rewrite the forwarded traceparent to name the request
	// span as parent (the splice). Inbound headers are left untouched.
	if pctx.Direction == pipeline.Outbound {
		p.propagator.Inject(trace.ContextWithSpanContext(ctx, sc), propagation.HeaderCarrier(pctx.Headers))
	}

	return sc, exchangeID
}

// stampedParent resolves the tracestate stamp on an outbound wire context:
// the inbound exchange id this pod's sidecar wrote into tracestate on the
// forwarded request, carried back by the app's shim. Returns ok=false when
// the member is absent or malformed (caller falls back to the map).
func stampedParent(rsc trace.SpanContext) (trace.SpanContext, bool) {
	raw := rsc.TraceState().Get(tracestateStampKey)
	if raw == "" {
		return trace.SpanContext{}, false
	}
	sid, err := trace.SpanIDFromHex(raw)
	if err != nil {
		return trace.SpanContext{}, false
	}
	psc := trace.NewSpanContext(trace.SpanContextConfig{
		TraceID:    rsc.TraceID(),
		SpanID:     sid,
		TraceFlags: rsc.TraceFlags(),
		Remote:     true,
	})
	return psc, psc.IsValid()
}

// OnResponse is a no-op. The response span is emitted in OnFinish (which fires
// on every finished exchange, including denials and abandonments), not here.
// The method exists only to satisfy the base pipeline.Plugin interface, which
// mandates OnResponse; it carries no logic in the two-span model.
func (p *LineageTelemetry) OnResponse(_ context.Context, _ *pipeline.Context) pipeline.Action {
	return pipeline.Action{Type: pipeline.Continue}
}

// OnFinish emits the response span — the twin of the request span, parented
// under it and echoing the same exchange.id — carrying outcome/status/output.
// Always fires at stream end, so a bodyless or failed exchange still completes
// as a first-class pair. Runs under a recover so an unexpected state never
// crashes the pipeline.
func (p *LineageTelemetry) OnFinish(ctx context.Context, pctx *pipeline.Context) {
	defer func() {
		if r := recover(); r != nil {
			slog.Warn("lineage-telemetry: OnFinish panic recovered", "recover", r)
		}
	}()

	state := pipeline.GetState[exchangeState](pctx, pluginName)
	if state == nil || !state.reqCtx.IsValid() {
		return
	}

	// Defer inbound span removal: the SSE connection from the client may be
	// cut by a proxy/ztunnel idle-timeout before the agent finishes
	// processing. Deleting immediately would break parent links for outbound
	// spans emitted after the client connection drops but while the turn is
	// still running. 5 minutes is well beyond any realistic turn duration.
	if state.inbound {
		traceID := state.traceID
		time.AfterFunc(5*time.Minute, func() { inboundSpans.Delete(traceID) })
	}

	outcome, status, hasStatus, deniedBy := lineageOutcome(pctx.Outcome())

	attrs := make([]attribute.KeyValue, 0, len(state.common)+5)
	attrs = append(attrs, attribute.String("lineage.role", "response"))
	attrs = append(attrs, state.common...)
	attrs = append(attrs, attribute.String("lineage.outcome", outcome))
	if hasStatus {
		attrs = append(attrs, attribute.Int("http.status_code", status))
	}
	if deniedBy != "" {
		attrs = append(attrs, attribute.String("lineage.denied_by", deniedBy))
	}
	if p.cfg.CaptureIO {
		if v := ioOutputValue(pctx, state.protocol); v != "" {
			attrs = append(attrs, attribute.String("output.value", v))
		}
	}

	parent := trace.ContextWithRemoteSpanContext(ctx, state.reqCtx)
	_, span := p.tracer.Start(parent, state.spanName+" response",
		trace.WithSpanKind(state.spanKind),
		trace.WithAttributes(attrs...),
	)
	span.End()
}

// lineageOutcome maps the pipeline's 3-value Outcome (allow/deny/error, nil
// outside OnFinish) onto the contract's lineage.outcome vocabulary
// (ok|denied|error|abandoned) plus the http.status_code fact. A terminal state
// with no status written (upstream reset, client disconnect, listener death)
// is "abandoned" — the row completes as in-flight-turned-failed rather than
// dangling. hasStatus is false when no status code was produced.
func lineageOutcome(o *pipeline.Outcome) (outcome string, status int, hasStatus bool, deniedBy string) {
	if o == nil {
		return "abandoned", 0, false, ""
	}
	switch o.FinalAction {
	case pipeline.OutcomeAllow:
		return "ok", o.StatusCode, o.StatusCode > 0, ""
	case pipeline.OutcomeDeny:
		return "denied", o.StatusCode, o.StatusCode > 0, o.DenyingPlugin
	case pipeline.OutcomeError:
		if o.StatusCode > 0 {
			return "error", o.StatusCode, true, ""
		}
		return "abandoned", 0, false, ""
	default:
		return "error", o.StatusCode, o.StatusCode > 0, ""
	}
}

// protocolOf reports which parser populated Extensions — the lineage.protocol
// fact. "http" means no parser matched.
func protocolOf(pctx *pipeline.Context) string {
	switch {
	case pctx.Extensions.A2A != nil:
		return "a2a"
	case pctx.Extensions.MCP != nil:
		return "mcp"
	case pctx.Extensions.Inference != nil:
		return "inference"
	default:
		return "http"
	}
}

// spanKindFor maps direction to OTel SpanKind: inbound is SERVER, outbound is
// CLIENT. The response span reuses its request span's kind.
func spanKindFor(dir pipeline.Direction) trace.SpanKind {
	if dir == pipeline.Inbound {
		return trace.SpanKindServer
	}
	return trace.SpanKindClient
}

// baseAttrs returns the facts carried on BOTH spans except exchange.id (added
// once the request span id is known) and role (differs per span).
func baseAttrs(pctx *pipeline.Context, self, protocol string) []attribute.KeyValue {
	attrs := []attribute.KeyValue{
		attribute.String("lineage.direction", pctx.Direction.String()),
		attribute.String("lineage.self.id", self),
		attribute.String("lineage.protocol", protocol),
	}
	// RemoteAddr is the direct TCP caller: on inbound that IS the peer; on
	// outbound it is the app's own socket (and empty under ext_proc), so it
	// is omitted rather than mislabeled — callee identity comes from peer.host.
	if pctx.Direction == pipeline.Inbound && pctx.RemoteAddr != "" {
		attrs = append(attrs, attribute.String("lineage.peer.addr", pctx.RemoteAddr))
	}
	if pctx.Host != "" {
		attrs = append(attrs, attribute.String("lineage.peer.host", pctx.Host))
	}
	return attrs
}

// appendRequestFacts adds the request-only facts: HTTP method/path, the
// protocol-specific parsed facts, validated-JWT principal (inbound only), and
// input.value when capture_io is on. protocolOf guarantees the matching
// extension pointer is non-nil.
func (p *LineageTelemetry) appendRequestFacts(attrs []attribute.KeyValue, pctx *pipeline.Context, protocol string) []attribute.KeyValue {
	if pctx.Method != "" {
		attrs = append(attrs, attribute.String("http.method", pctx.Method))
	}
	if pctx.Path != "" {
		attrs = append(attrs, attribute.String("url.path", pctx.Path))
	}
	switch protocol {
	case "a2a":
		a := pctx.Extensions.A2A
		if a.Method != "" {
			attrs = append(attrs, attribute.String("a2a.method", a.Method))
		}
		if a.SessionID != "" {
			attrs = append(attrs, attribute.String("a2a.session_id", a.SessionID))
		}
	case "mcp":
		m := pctx.Extensions.MCP
		if m.Method != "" {
			attrs = append(attrs, attribute.String("mcp.method", m.Method))
		}
		if t := mcpTool(pctx); t != "" {
			attrs = append(attrs, attribute.String("mcp.tool", t))
		}
	case "inference":
		if model := pctx.Extensions.Inference.Model; model != "" {
			attrs = append(attrs, attribute.String("inference.model", model))
		}
	}
	// Principal facts: request span, inbound only, and only from a validated
	// JWT (pctx.Identity non-nil).
	if pctx.Direction == pipeline.Inbound && pctx.Identity != nil {
		if s := pctx.Identity.Subject(); s != "" {
			attrs = append(attrs, attribute.String("lineage.principal.sub", s))
		}
		if c := pctx.Identity.ClientID(); c != "" {
			attrs = append(attrs, attribute.String("lineage.principal.client", c))
		}
	}
	if p.cfg.CaptureIO {
		if v := ioInputValue(pctx, protocol); v != "" {
			attrs = append(attrs, attribute.String("input.value", v))
		}
	}
	return attrs
}

// requestSpanName builds "{self} {protocol} {op}", dropping the trailing op
// when it is empty. The response span appends " response".
func requestSpanName(self, protocol, op string) string {
	if op == "" {
		return self + " " + protocol
	}
	return self + " " + protocol + " " + op
}

// spanOp picks the operation label for the span name per protocol:
// mcp.tool / a2a.method / inference.model, falling back to url.path.
func spanOp(pctx *pipeline.Context, protocol string) string {
	var op string
	switch protocol {
	case "a2a":
		if pctx.Extensions.A2A != nil {
			op = pctx.Extensions.A2A.Method
		}
	case "mcp":
		op = mcpTool(pctx)
		if op == "" && pctx.Extensions.MCP != nil {
			op = pctx.Extensions.MCP.Method
		}
	case "inference":
		if pctx.Extensions.Inference != nil {
			op = pctx.Extensions.Inference.Model
		}
	}
	if op == "" {
		op = pctx.Path
	}
	return op
}

// mcpTool returns the tool name for an MCP tools/call, or "" otherwise.
func mcpTool(pctx *pipeline.Context) string {
	m := pctx.Extensions.MCP
	if m == nil || m.Method != "tools/call" || m.Params == nil {
		return ""
	}
	if name, ok := m.Params["name"].(string); ok {
		return name
	}
	return ""
}

// serviceLabel reduces a SPIFFE ID to its last path segment, or returns
// selfID as-is if it is not a SPIFFE URI. Falls back to "agent" if empty.
// Used for the lineage.self.id fact and span names.
//
//	"spiffe://trust-domain/ns/team1/sa/weather-service" → "weather-service"
//	"weather-service" → "weather-service"
func serviceLabel(selfID string) string {
	if selfID == "" {
		return "agent"
	}
	parts := strings.Split(selfID, "/")
	for i := len(parts) - 1; i >= 0; i-- {
		if parts[i] != "" {
			return parts[i]
		}
	}
	return selfID
}

// ioInputValue returns the input.value for a request span: the parsed request
// content for *protocol* — the hop's lineage.protocol fact — or "" if that
// parser produced nothing meaningful. Only that protocol's extension is read:
// parsers are precedence-ordered, not mutually exclusive (mcp-parser matches
// any JSON-RPC body, including every a2a exchange), so falling through to
// another parser's output would attach a mislabeled protocol envelope. A hop
// whose own parser yields nothing keeps a NULL payload — the contract's
// "interactions are independent of payloads".
func ioInputValue(pctx *pipeline.Context, protocol string) string {
	ext := pctx.Extensions
	switch {
	case protocol == "a2a" && ext.A2A != nil && len(ext.A2A.Parts) > 0:
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
	case protocol == "inference" && ext.Inference != nil && len(ext.Inference.Messages) > 0:
		if b, err := json.Marshal(ext.Inference.Messages); err == nil {
			return string(b)
		}
	case protocol == "mcp" && ext.MCP != nil && ext.MCP.Params != nil:
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

// ioOutputValue returns the output.value for a response span: the parsed
// response content for *protocol* — the REQUEST span's lineage.protocol fact —
// or "" if that parser produced nothing. Only that protocol's extension is
// read, for the same reason as ioInputValue: mcp-parser also parses every a2a
// response (any JSON-RPC body), and falling through to it would emit the raw
// JSON-RPC envelope — including the protocol events isA2AProtocolEvent exists
// to suppress — as an a2a hop's payload.
func ioOutputValue(pctx *pipeline.Context, protocol string) string {
	ext := pctx.Extensions
	switch {
	case protocol == "a2a" && ext.A2A != nil && ext.A2A.Artifact != "" && !isA2AProtocolEvent(ext.A2A.Artifact):
		return ext.A2A.Artifact
	case protocol == "a2a" && ext.A2A != nil && ext.A2A.ErrorMessage != "":
		return ext.A2A.ErrorMessage
	case protocol == "inference" && ext.Inference != nil && ext.Inference.Completion != "":
		return ext.Inference.Completion
	case protocol == "inference" && ext.Inference != nil && len(ext.Inference.ToolCalls) > 0:
		if b, err := json.Marshal(ext.Inference.ToolCalls); err == nil {
			return string(b)
		}
	case protocol == "mcp" && ext.MCP != nil && ext.MCP.Result != nil:
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
	case protocol == "mcp" && ext.MCP != nil && ext.MCP.Err != nil:
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
