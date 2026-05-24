package lineage

import (
	"context"
	"encoding/json"
	"net/http"
	"testing"

	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"

	"github.com/kagenti/kagenti-extensions/authbridge/authlib/pipeline"
)

// newTestPlugin creates a LineageTelemetry wired to an in-memory span exporter
// and marks it ready so Init is not needed.
func newTestPlugin(t *testing.T) (*LineageTelemetry, *tracetest.InMemoryExporter) {
	t.Helper()
	exp := tracetest.NewInMemoryExporter()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSyncer(exp))
	p := NewLineageTelemetry()
	p.tp = tp
	p.tracer = tp.Tracer("test")
	p.ready.Store(true)
	return p, exp
}

// runWith builds a single-plugin pipeline, calls Run then RunFinish, and
// returns the exported spans. outcome can be zero-value to use OutcomeAllow.
func runWith(t *testing.T, p *LineageTelemetry, pctx *pipeline.Context, outcome pipeline.Outcome) []tracetest.SpanStub {
	t.Helper()
	pl, err := pipeline.New([]pipeline.Plugin{p})
	if err != nil {
		t.Fatalf("pipeline.New: %v", err)
	}
	pl.Run(context.Background(), pctx)
	pl.RunFinish(context.Background(), pctx, outcome)
	return nil // spans come from the exporter, returned by caller
}

func fakeContext(dir pipeline.Direction, headers http.Header) *pipeline.Context {
	return &pipeline.Context{
		Direction: dir,
		Host:      "test-service",
		Path:      "/test",
		Headers:   headers,
	}
}

func TestOnRequest_InboundHop(t *testing.T) {
	p, exp := newTestPlugin(t)
	pctx := fakeContext(pipeline.Inbound, http.Header{})

	pl, err := pipeline.New([]pipeline.Plugin{p})
	if err != nil {
		t.Fatalf("pipeline.New: %v", err)
	}

	action := pl.Run(context.Background(), pctx)
	if action.Type != pipeline.Continue {
		t.Fatalf("expected Continue, got %v", action.Type)
	}

	state := pipeline.GetState[hopState](pctx, pluginName)
	if state == nil {
		t.Fatal("expected hopState to be set after OnRequest")
	}
	if state.info.Kind != HopPrincipalToAgent {
		t.Errorf("hop kind = %s, want principal_to_agent", state.info.Kind)
	}
	if state.info.Protocol != "http" {
		t.Errorf("protocol = %s, want http", state.info.Protocol)
	}

	// Span not yet exported — still open.
	if len(exp.GetSpans()) != 0 {
		t.Errorf("span exported before OnFinish: got %d", len(exp.GetSpans()))
	}
}

func TestOnFinish_Allow(t *testing.T) {
	p, exp := newTestPlugin(t)
	pctx := fakeContext(pipeline.Inbound, http.Header{})
	pctx.StatusCode = 200

	pl, err := pipeline.New([]pipeline.Plugin{p})
	if err != nil {
		t.Fatalf("pipeline.New: %v", err)
	}
	pl.Run(context.Background(), pctx)
	pl.RunFinish(context.Background(), pctx, pipeline.Outcome{
		FinalAction: pipeline.OutcomeAllow,
		StatusCode:  200,
	})

	spans := exp.GetSpans()
	if len(spans) != 1 {
		t.Fatalf("expected 1 exported span, got %d", len(spans))
	}
	checkAttr(t, spans[0], "lineage.hop.kind", "principal_to_agent")
	checkAttr(t, spans[0], "lineage.outcome", "allow")
	checkAttr(t, spans[0], "http.status_code", int64(200))
}

func TestOnFinish_Deny(t *testing.T) {
	p, exp := newTestPlugin(t)
	pctx := fakeContext(pipeline.Inbound, http.Header{})
	pctx.StatusCode = 401

	pl, err := pipeline.New([]pipeline.Plugin{p})
	if err != nil {
		t.Fatalf("pipeline.New: %v", err)
	}
	pl.Run(context.Background(), pctx)
	pl.RunFinish(context.Background(), pctx, pipeline.Outcome{
		FinalAction:   pipeline.OutcomeDeny,
		StatusCode:    401,
		DenyingPlugin: "jwt-validation",
	})

	spans := exp.GetSpans()
	if len(spans) != 1 {
		t.Fatalf("expected 1 exported span, got %d", len(spans))
	}
	checkAttr(t, spans[0], "lineage.outcome", "deny")
	checkAttr(t, spans[0], "lineage.denying_plugin", "jwt-validation")
	checkAttr(t, spans[0], "http.status_code", int64(401))
}

func TestOnFinish_MCP(t *testing.T) {
	p, exp := newTestPlugin(t)
	pctx := fakeContext(pipeline.Outbound, http.Header{})
	pctx.Extensions.MCP = &pipeline.MCPExtension{Method: "tools/call"}
	pctx.StatusCode = 200

	pl, err := pipeline.New([]pipeline.Plugin{p})
	if err != nil {
		t.Fatalf("pipeline.New: %v", err)
	}
	pl.Run(context.Background(), pctx)
	pl.RunFinish(context.Background(), pctx, pipeline.Outcome{
		FinalAction: pipeline.OutcomeAllow,
		StatusCode:  200,
	})

	spans := exp.GetSpans()
	if len(spans) != 1 {
		t.Fatalf("expected 1 span, got %d", len(spans))
	}
	checkAttr(t, spans[0], "lineage.hop.kind", "agent_to_tool")
	checkAttr(t, spans[0], "mcp.method", "tools/call")
}

func TestOnFinish_A2A(t *testing.T) {
	p, exp := newTestPlugin(t)
	pctx := fakeContext(pipeline.Outbound, http.Header{})
	pctx.Extensions.A2A = &pipeline.A2AExtension{
		Method:    "tasks/send",
		SessionID: "sess-123",
	}
	pctx.StatusCode = 200

	pl, err := pipeline.New([]pipeline.Plugin{p})
	if err != nil {
		t.Fatalf("pipeline.New: %v", err)
	}
	pl.Run(context.Background(), pctx)
	pl.RunFinish(context.Background(), pctx, pipeline.Outcome{
		FinalAction: pipeline.OutcomeAllow,
		StatusCode:  200,
	})

	spans := exp.GetSpans()
	if len(spans) != 1 {
		t.Fatalf("expected 1 span, got %d", len(spans))
	}
	checkAttr(t, spans[0], "lineage.hop.kind", "agent_to_agent")
	checkAttr(t, spans[0], "a2a.method", "tasks/send")
	checkAttr(t, spans[0], "a2a.session_id", "sess-123")
}

func TestOnFinish_Inference(t *testing.T) {
	p, exp := newTestPlugin(t)
	pctx := fakeContext(pipeline.Outbound, http.Header{})
	pctx.Extensions.Inference = &pipeline.InferenceExtension{Model: "gpt-4o"}
	pctx.StatusCode = 200

	pl, err := pipeline.New([]pipeline.Plugin{p})
	if err != nil {
		t.Fatalf("pipeline.New: %v", err)
	}
	pl.Run(context.Background(), pctx)
	pl.RunFinish(context.Background(), pctx, pipeline.Outcome{
		FinalAction: pipeline.OutcomeAllow,
		StatusCode:  200,
	})

	spans := exp.GetSpans()
	if len(spans) != 1 {
		t.Fatalf("expected 1 span, got %d", len(spans))
	}
	checkAttr(t, spans[0], "lineage.hop.kind", "agent_to_llm")
	checkAttr(t, spans[0], "inference.model", "gpt-4o")
}

func TestOnFinish_NoStatePanic(t *testing.T) {
	p, _ := newTestPlugin(t)
	pctx := fakeContext(pipeline.Inbound, http.Header{})
	// OnFinish without OnRequest having run — no hopState stored.
	p.OnFinish(context.Background(), pctx) // must not panic
}

func TestNotReady_SkipsSpan(t *testing.T) {
	p := NewLineageTelemetry()
	// Do NOT set ready — Init never called.
	pctx := fakeContext(pipeline.Inbound, http.Header{})
	action := p.OnRequest(context.Background(), pctx)
	if action.Type != pipeline.Continue {
		t.Fatalf("expected Continue, got %v", action.Type)
	}
	state := pipeline.GetState[hopState](pctx, pluginName)
	if state != nil {
		t.Error("hopState should not be set when plugin is not ready")
	}
}

func TestConfigure(t *testing.T) {
	p := NewLineageTelemetry()
	raw := json.RawMessage(`{"otel_endpoint":"collector:4317","emit_body_hash":true}`)
	if err := p.Configure(raw); err != nil {
		t.Fatalf("Configure: %v", err)
	}
	if p.cfg.OTelEndpoint != "collector:4317" {
		t.Errorf("endpoint = %q, want %q", p.cfg.OTelEndpoint, "collector:4317")
	}
	if !p.cfg.EmitBodyHash {
		t.Error("emit_body_hash should be true")
	}
}

func TestConfigure_StripScheme(t *testing.T) {
	p := NewLineageTelemetry()
	raw := json.RawMessage(`{"otel_endpoint":"http://collector:4317"}`)
	if err := p.Configure(raw); err != nil {
		t.Fatalf("Configure: %v", err)
	}
	if p.cfg.OTelEndpoint != "collector:4317" {
		t.Errorf("endpoint = %q, want %q", p.cfg.OTelEndpoint, "collector:4317")
	}
}

func TestConfigure_Defaults(t *testing.T) {
	p := NewLineageTelemetry()
	if err := p.Configure(nil); err != nil {
		t.Fatalf("Configure(nil): %v", err)
	}
	if p.cfg.OTelEndpoint != "localhost:4317" {
		t.Errorf("default endpoint = %q, want localhost:4317", p.cfg.OTelEndpoint)
	}
}

func TestDetermineHop(t *testing.T) {
	cases := []struct {
		name     string
		dir      pipeline.Direction
		ext      func(*pipeline.Context)
		wantKind HopKind
		wantProt string
	}{
		{"inbound", pipeline.Inbound, nil, HopPrincipalToAgent, "http"},
		{"outbound_a2a", pipeline.Outbound, func(p *pipeline.Context) { p.Extensions.A2A = &pipeline.A2AExtension{} }, HopAgentToAgent, "a2a"},
		{"outbound_mcp", pipeline.Outbound, func(p *pipeline.Context) { p.Extensions.MCP = &pipeline.MCPExtension{} }, HopAgentToTool, "mcp"},
		{"outbound_inf", pipeline.Outbound, func(p *pipeline.Context) { p.Extensions.Inference = &pipeline.InferenceExtension{} }, HopAgentToLLM, "inference"},
		{"outbound_none", pipeline.Outbound, nil, HopAgentToService, "http"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			pctx := &pipeline.Context{Direction: tc.dir}
			if tc.ext != nil {
				tc.ext(pctx)
			}
			info := determineHop(pctx)
			if info.Kind != tc.wantKind {
				t.Errorf("Kind = %s, want %s", info.Kind, tc.wantKind)
			}
			if info.Protocol != tc.wantProt {
				t.Errorf("Protocol = %s, want %s", info.Protocol, tc.wantProt)
			}
		})
	}
}

// checkAttr asserts a span contains attribute key=value.
func checkAttr(t *testing.T, span tracetest.SpanStub, key string, wantVal any) {
	t.Helper()
	for _, attr := range span.Attributes {
		if string(attr.Key) != key {
			continue
		}
		got := attr.Value.AsInterface()
		if got != wantVal {
			t.Errorf("attr %q = %v (%T), want %v (%T)", key, got, got, wantVal, wantVal)
		}
		return
	}
	t.Errorf("attribute %q not found in span %q", key, span.Name)
}
