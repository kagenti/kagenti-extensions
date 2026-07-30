package lineage

import (
	"context"
	"encoding/json"
	"maps"
	"net/http"
	"strings"
	"testing"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/propagation"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"

	"github.com/kagenti/kagenti-extensions/authbridge/authlib/pipeline"
)

// newTestPlugin creates a LineageTelemetry wired to an in-memory span exporter
// (synchronous, so a span appears the instant it is ended) and marks it ready
// so Init is not needed.
func newTestPlugin(t *testing.T) (*LineageTelemetry, *tracetest.InMemoryExporter) {
	t.Helper()
	clearInboundSpans()
	exp := tracetest.NewInMemoryExporter()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSyncer(exp))
	p := NewLineageTelemetry()
	p.tp = tp
	p.tracer = tp.Tracer("test")
	p.selfID = "weather-service"
	p.ready.Store(true)
	return p, exp
}

// clearInboundSpans drains the process-global map so tests don't leak
// parent links into one another.
func clearInboundSpans() {
	inboundSpans.Range(func(k, _ any) bool {
		inboundSpans.Delete(k)
		return true
	})
}

// run drives a full exchange (request pass + finish) through a single-plugin
// pipeline. Spans are read from the caller's exporter.
func run(t *testing.T, p *LineageTelemetry, pctx *pipeline.Context, outcome pipeline.Outcome) {
	t.Helper()
	pl, err := pipeline.New([]pipeline.Plugin{p})
	if err != nil {
		t.Fatalf("pipeline.New: %v", err)
	}
	pl.Run(context.Background(), pctx)
	pl.RunFinish(context.Background(), pctx, outcome)
}

// allow is the ordinary success outcome.
func allow(status int) pipeline.Outcome {
	return pipeline.Outcome{FinalAction: pipeline.OutcomeAllow, StatusCode: status}
}

func fakeContext(dir pipeline.Direction, headers http.Header) *pipeline.Context {
	return &pipeline.Context{
		Direction: dir,
		Method:    "POST",
		Host:      "test-service:8000",
		Path:      "/test",
		Headers:   headers,
	}
}

// traceparent builds a header carrier naming traceID/spanID as the wire parent.
func traceparent(traceID, spanID string) http.Header {
	h := http.Header{}
	h.Set("traceparent", "00-"+traceID+"-"+spanID+"-01")
	return h
}

// extractParent decodes the span context named by the headers' traceparent.
func extractParent(h http.Header) trace.SpanContext {
	ctx := propagation.TraceContext{}.Extract(context.Background(), propagation.HeaderCarrier(h))
	return trace.SpanContextFromContext(ctx)
}

// roleSplit returns the request and response spans from an exported set,
// asserting exactly one of each.
func roleSplit(t *testing.T, spans tracetest.SpanStubs) (req, resp tracetest.SpanStub) {
	t.Helper()
	var gotReq, gotResp bool
	for _, s := range spans {
		switch attrStr(s, "lineage.role") {
		case "request":
			if gotReq {
				t.Fatal("more than one request span")
			}
			req, gotReq = s, true
		case "response":
			if gotResp {
				t.Fatal("more than one response span")
			}
			resp, gotResp = s, true
		default:
			t.Fatalf("span %q has no lineage.role", s.Name)
		}
	}
	if !gotReq || !gotResp {
		t.Fatalf("want one request + one response span, got %d spans (req=%v resp=%v)", len(spans), gotReq, gotResp)
	}
	return req, resp
}

// ---- identifiers, pairing, parenting ----

func TestExchange_TwoSpansPairedAndParented(t *testing.T) {
	p, exp := newTestPlugin(t)
	pctx := fakeContext(pipeline.Inbound, http.Header{})

	pl, err := pipeline.New([]pipeline.Plugin{p})
	if err != nil {
		t.Fatalf("pipeline.New: %v", err)
	}

	// Emit on sight: the request span exists after the request pass, before finish.
	pl.Run(context.Background(), pctx)
	if got := len(exp.GetSpans()); got != 1 {
		t.Fatalf("after request pass: want 1 span (request), got %d", got)
	}

	pl.RunFinish(context.Background(), pctx, allow(200))
	spans := exp.GetSpans()
	if len(spans) != 2 {
		t.Fatalf("after finish: want 2 spans, got %d", len(spans))
	}
	req, resp := roleSplit(t, spans)

	// exchange.id == request span id, echoed on both.
	wantID := req.SpanContext.SpanID().String()
	if got := attrStr(req, "lineage.exchange.id"); got != wantID {
		t.Errorf("request exchange.id = %q, want %q", got, wantID)
	}
	if got := attrStr(resp, "lineage.exchange.id"); got != wantID {
		t.Errorf("response exchange.id = %q, want %q", got, wantID)
	}

	// Response span's parent is the request span (same trace).
	if resp.Parent.SpanID() != req.SpanContext.SpanID() {
		t.Errorf("response parent span = %s, want request span %s", resp.Parent.SpanID(), req.SpanContext.SpanID())
	}
	if resp.SpanContext.TraceID() != req.SpanContext.TraceID() {
		t.Errorf("response trace = %s, want request trace %s", resp.SpanContext.TraceID(), req.SpanContext.TraceID())
	}

	// Both spans share the same SpanKind (SERVER for inbound).
	if req.SpanKind != trace.SpanKindServer || resp.SpanKind != trace.SpanKindServer {
		t.Errorf("span kinds = %v/%v, want server/server", req.SpanKind, resp.SpanKind)
	}
}

// ---- the splice ----

func TestSplice_OutboundHeaderRewritten(t *testing.T) {
	p, exp := newTestPlugin(t)
	const traceID, wireParent = "4bf92f3577b34da6a3ce929d0e0e4736", "00f067aa0ba902b7"
	h := traceparent(traceID, wireParent)
	pctx := fakeContext(pipeline.Outbound, h)
	pctx.Extensions.MCP = &pipeline.MCPExtension{Method: "tools/call", Params: map[string]any{"name": "get_weather"}}

	run(t, p, pctx, allow(200))

	req, _ := roleSplit(t, exp.GetSpans())
	// The forwarded traceparent now names the request span (not the wire parent).
	forwarded := extractParent(pctx.Headers)
	if forwarded.SpanID() != req.SpanContext.SpanID() {
		t.Errorf("forwarded parent = %s, want request span %s", forwarded.SpanID(), req.SpanContext.SpanID())
	}
	if forwarded.SpanID().String() == wireParent {
		t.Error("forwarded parent was left as the wire parent — splice did not apply")
	}
	if got := forwarded.TraceID().String(); got != traceID {
		t.Errorf("forwarded trace = %s, want %s (splice must keep the trace)", got, traceID)
	}
}

func TestSplice_InboundHeadersUntouchedExceptStamp(t *testing.T) {
	p, exp := newTestPlugin(t)
	h := traceparent("4bf92f3577b34da6a3ce929d0e0e4736", "00f067aa0ba902b7")
	before := http.Header{}
	maps.Copy(before, h)
	pctx := fakeContext(pipeline.Inbound, h)

	run(t, p, pctx, allow(200))

	// The ONLY inbound mutation is the tracestate stamp; traceparent and
	// everything else are forwarded as they arrived.
	req, _ := roleSplit(t, exp.GetSpans())
	want := tracestateStampKey + "=" + req.SpanContext.SpanID().String()
	if got := pctx.Headers.Get("tracestate"); got != want {
		t.Errorf("tracestate = %q, want stamp %q", got, want)
	}
	after := http.Header{}
	maps.Copy(after, pctx.Headers)
	after.Del("tracestate")
	if !headersEqual(before, after) {
		t.Errorf("inbound headers beyond tracestate mutated: before=%v after=%v", before, after)
	}
}

func TestStamp_PreservesForeignTracestateMembers(t *testing.T) {
	p, exp := newTestPlugin(t)
	h := traceparent("4bf92f3577b34da6a3ce929d0e0e4736", "00f067aa0ba902b7")
	h.Set("tracestate", "vendor=abc")
	pctx := fakeContext(pipeline.Inbound, h)

	run(t, p, pctx, allow(200))

	req, _ := roleSplit(t, exp.GetSpans())
	got := pctx.Headers.Get("tracestate")
	wantStamp := tracestateStampKey + "=" + req.SpanContext.SpanID().String()
	if !strings.Contains(got, wantStamp) || !strings.Contains(got, "vendor=abc") {
		t.Errorf("tracestate = %q, want both %q and vendor=abc", got, wantStamp)
	}
}

func TestStamp_NoWireTraceparentNoStamp(t *testing.T) {
	p, _ := newTestPlugin(t)
	pctx := fakeContext(pipeline.Inbound, http.Header{})

	run(t, p, pctx, allow(200))

	if got := pctx.Headers.Get("tracestate"); got != "" {
		t.Errorf("tracestate stamped without a wire traceparent: %q", got)
	}
}

// TestStamp_OutboundPrefersStampOverMap is the same-trace fan-in case in
// miniature: two concurrent inbound exchanges on ONE trace (the trace-keyed
// map can only hold the later one), then an outbound whose tracestate stamp
// names the EARLIER inbound. Without the stamp this outbound would collapse
// onto the map entry — the 1/N misattribution the fanin-test.sh e2e proves.
func TestStamp_OutboundPrefersStampOverMap(t *testing.T) {
	p, exp := newTestPlugin(t)
	const traceID = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

	// Two inbounds, same trace: the map now holds in2 only.
	run(t, p, fakeContext(pipeline.Inbound, traceparent(traceID, "1111111111111111")), allow(200))
	in1, _ := roleSplit(t, exp.GetSpans())
	exp.Reset()
	run(t, p, fakeContext(pipeline.Inbound, traceparent(traceID, "2222222222222222")), allow(200))
	in2, _ := roleSplit(t, exp.GetSpans())

	// Outbound couriered in1's stamp through the app.
	exp.Reset()
	h := traceparent(traceID, "3333333333333333")
	h.Set("tracestate", tracestateStampKey+"="+in1.SpanContext.SpanID().String())
	out := fakeContext(pipeline.Outbound, h)
	run(t, p, out, allow(200))
	outReq, _ := roleSplit(t, exp.GetSpans())

	if outReq.Parent.SpanID() != in1.SpanContext.SpanID() {
		t.Errorf("parent = %s, want stamped inbound %s (map held %s)",
			outReq.Parent.SpanID(), in1.SpanContext.SpanID(), in2.SpanContext.SpanID())
	}
	if got := attrStr(outReq, "lineage.parent.source"); got != "tracestate" {
		t.Errorf("lineage.parent.source = %q, want tracestate", got)
	}
}

func TestStamp_MalformedFallsBackToMap(t *testing.T) {
	p, exp := newTestPlugin(t)
	const traceID = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

	run(t, p, fakeContext(pipeline.Inbound, traceparent(traceID, "1111111111111111")), allow(200))
	in1, _ := roleSplit(t, exp.GetSpans())

	exp.Reset()
	h := traceparent(traceID, "3333333333333333")
	h.Set("tracestate", tracestateStampKey+"=nothex")
	out := fakeContext(pipeline.Outbound, h)
	run(t, p, out, allow(200))
	outReq, _ := roleSplit(t, exp.GetSpans())

	if outReq.Parent.SpanID() != in1.SpanContext.SpanID() {
		t.Errorf("parent = %s, want map inbound %s", outReq.Parent.SpanID(), in1.SpanContext.SpanID())
	}
	if got := attrStr(outReq, "lineage.parent.source"); got != "map" {
		t.Errorf("lineage.parent.source = %q, want map", got)
	}
}

func TestStamp_ParentSourceWireOnMapMiss(t *testing.T) {
	p, exp := newTestPlugin(t)
	out := fakeContext(pipeline.Outbound, traceparent("cccccccccccccccccccccccccccccccc", "1111111111111111"))
	run(t, p, out, allow(200))
	outReq, _ := roleSplit(t, exp.GetSpans())
	if got := attrStr(outReq, "lineage.parent.source"); got != "wire" {
		t.Errorf("lineage.parent.source = %q, want wire", got)
	}
}

func TestSplice_ParentMapHitVsMiss(t *testing.T) {
	p, exp := newTestPlugin(t)
	const traceID = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

	// Miss: outbound on a trace with no inbound span → parent is the wire parent.
	const wireParent = "1111111111111111"
	missCtx := fakeContext(pipeline.Outbound, traceparent(traceID, wireParent))
	run(t, p, missCtx, allow(200))
	missReq, _ := roleSplit(t, exp.GetSpans())
	if got := missReq.Parent.SpanID().String(); got != wireParent {
		t.Errorf("map miss: parent = %s, want wire parent %s", got, wireParent)
	}

	// Establish this pod's inbound span for the trace.
	exp.Reset()
	inCtx := fakeContext(pipeline.Inbound, traceparent(traceID, "2222222222222222"))
	run(t, p, inCtx, allow(200))
	inReq, _ := roleSplit(t, exp.GetSpans())
	inboundSpanID := inReq.SpanContext.SpanID()

	// Hit: outbound on the same trace re-parents under the inbound span,
	// ignoring the wire parent it arrived with.
	exp.Reset()
	hitCtx := fakeContext(pipeline.Outbound, traceparent(traceID, "3333333333333333"))
	run(t, p, hitCtx, allow(200))
	hitReq, _ := roleSplit(t, exp.GetSpans())
	if hitReq.Parent.SpanID() != inboundSpanID {
		t.Errorf("map hit: parent = %s, want inbound span %s", hitReq.Parent.SpanID(), inboundSpanID)
	}
}

func TestSplice_ConcurrentTracesNeverCross(t *testing.T) {
	p, exp := newTestPlugin(t)
	const traceA = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	const traceB = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

	// Two inbound spans on two traces.
	run(t, p, fakeContext(pipeline.Inbound, traceparent(traceA, "1111111111111111")), allow(200))
	inA, _ := roleSplit(t, exp.GetSpans())
	exp.Reset()
	run(t, p, fakeContext(pipeline.Inbound, traceparent(traceB, "2222222222222222")), allow(200))
	inB, _ := roleSplit(t, exp.GetSpans())

	// Outbound on trace A parents under inbound A, never B.
	exp.Reset()
	run(t, p, fakeContext(pipeline.Outbound, traceparent(traceA, "3333333333333333")), allow(200))
	outA, _ := roleSplit(t, exp.GetSpans())
	if outA.Parent.SpanID() != inA.SpanContext.SpanID() {
		t.Errorf("outbound A parent = %s, want inbound A %s", outA.Parent.SpanID(), inA.SpanContext.SpanID())
	}
	if outA.Parent.SpanID() == inB.SpanContext.SpanID() {
		t.Error("outbound A crossed into inbound B's span")
	}

	// Outbound on trace B parents under inbound B.
	exp.Reset()
	run(t, p, fakeContext(pipeline.Outbound, traceparent(traceB, "4444444444444444")), allow(200))
	outB, _ := roleSplit(t, exp.GetSpans())
	if outB.Parent.SpanID() != inB.SpanContext.SpanID() {
		t.Errorf("outbound B parent = %s, want inbound B %s", outB.Parent.SpanID(), inB.SpanContext.SpanID())
	}
}

// ---- bodyless / unparsed completeness ----

func TestBodyless_UnparsedNoCaptureStillEmitsBothSpans(t *testing.T) {
	p, exp := newTestPlugin(t)
	// capture_io defaults false; no parser extensions → protocol http.
	pctx := fakeContext(pipeline.Outbound, http.Header{})

	run(t, p, pctx, allow(200))

	req, resp := roleSplit(t, exp.GetSpans())
	if got := attrStr(req, "lineage.protocol"); got != "http" {
		t.Errorf("protocol = %q, want http", got)
	}
	// Complete: both carry the shared facts and the exchange is paired.
	if attrStr(req, "lineage.exchange.id") == "" || attrStr(resp, "lineage.exchange.id") == "" {
		t.Error("exchange.id missing on a bodyless span")
	}
	if got := attrStr(resp, "lineage.outcome"); got != "ok" {
		t.Errorf("outcome = %q, want ok", got)
	}
	// No payloads captured.
	if _, ok := findAttr(req, "input.value"); ok {
		t.Error("input.value present with capture_io off")
	}
	if _, ok := findAttr(resp, "output.value"); ok {
		t.Error("output.value present with capture_io off")
	}
}

// ---- outcomes ----

func TestOutcome_Denied(t *testing.T) {
	p, exp := newTestPlugin(t)
	pctx := fakeContext(pipeline.Inbound, http.Header{})

	run(t, p, pctx, pipeline.Outcome{
		FinalAction:   pipeline.OutcomeDeny,
		StatusCode:    401,
		DenyingPlugin: "jwt-validation",
	})

	_, resp := roleSplit(t, exp.GetSpans())
	if got := attrStr(resp, "lineage.outcome"); got != "denied" {
		t.Errorf("outcome = %q, want denied", got)
	}
	if got := attrStr(resp, "lineage.denied_by"); got != "jwt-validation" {
		t.Errorf("denied_by = %q, want jwt-validation", got)
	}
	if got, ok := intAttr(resp, "http.status_code"); !ok || got != 401 {
		t.Errorf("http.status_code = %d (ok=%v), want 401", got, ok)
	}
}

func TestOutcome_AbandonedHasNoStatus(t *testing.T) {
	p, exp := newTestPlugin(t)
	pctx := fakeContext(pipeline.Outbound, http.Header{})

	// Terminal error with no response written (upstream reset / disconnect).
	run(t, p, pctx, pipeline.Outcome{FinalAction: pipeline.OutcomeError, StatusCode: 0})

	_, resp := roleSplit(t, exp.GetSpans())
	if got := attrStr(resp, "lineage.outcome"); got != "abandoned" {
		t.Errorf("outcome = %q, want abandoned", got)
	}
	if _, ok := findAttr(resp, "http.status_code"); ok {
		t.Error("http.status_code present on an abandoned exchange (none was produced)")
	}
}

// ---- request facts + capture_io + span names ----

func TestRequestFacts_MCPWithCapture(t *testing.T) {
	p, exp := newTestPlugin(t)
	p.cfg.CaptureIO = true
	pctx := fakeContext(pipeline.Outbound, http.Header{})
	pctx.Host = "weather-tool-mcp.team1.svc:8000"
	pctx.Path = "/mcp"
	pctx.RemoteAddr = "10.244.1.7:8000"
	pctx.Extensions.MCP = &pipeline.MCPExtension{
		Method: "tools/call",
		Params: map[string]any{"name": "get_weather", "arguments": map[string]any{"city": "Tokyo"}},
		Result: map[string]any{"content": []any{map[string]any{"type": "text", "text": "sunny"}}},
	}

	run(t, p, pctx, allow(200))
	req, resp := roleSplit(t, exp.GetSpans())

	checkAttr(t, req, "lineage.protocol", "mcp")
	checkAttr(t, req, "mcp.method", "tools/call")
	checkAttr(t, req, "mcp.tool", "get_weather")
	checkAttr(t, req, "http.method", "POST")
	checkAttr(t, req, "url.path", "/mcp")
	checkAttr(t, req, "lineage.self.id", "weather-service")
	checkAttr(t, req, "lineage.peer.host", "weather-tool-mcp.team1.svc:8000")
	// peer.addr is inbound-only: on outbound, RemoteAddr is the app's own
	// socket (or empty under ext_proc) — not the peer.
	if _, ok := findAttr(req, "lineage.peer.addr"); ok {
		t.Error("lineage.peer.addr emitted on outbound — it names the app there, not the peer")
	}
	checkAttr(t, req, "lineage.direction", "outbound")
	checkAttr(t, req, "input.value", `{"city":"Tokyo"}`)
	checkAttr(t, resp, "output.value", "sunny")

	if req.Name != "weather-service mcp get_weather" {
		t.Errorf("request span name = %q", req.Name)
	}
	if resp.Name != "weather-service mcp get_weather response" {
		t.Errorf("response span name = %q", resp.Name)
	}
	if req.SpanKind != trace.SpanKindClient {
		t.Errorf("outbound request kind = %v, want client", req.SpanKind)
	}
}

func TestRequestFacts_A2AAndInference(t *testing.T) {
	p, exp := newTestPlugin(t)
	// A2A.
	a := fakeContext(pipeline.Outbound, http.Header{})
	a.Extensions.A2A = &pipeline.A2AExtension{Method: "message/send", SessionID: "sess-123"}
	run(t, p, a, allow(200))
	areq, _ := roleSplit(t, exp.GetSpans())
	checkAttr(t, areq, "lineage.protocol", "a2a")
	checkAttr(t, areq, "a2a.method", "message/send")
	checkAttr(t, areq, "a2a.session_id", "sess-123")
	if areq.Name != "weather-service a2a message/send" {
		t.Errorf("a2a span name = %q", areq.Name)
	}

	// Inference.
	exp.Reset()
	i := fakeContext(pipeline.Outbound, http.Header{})
	i.Extensions.Inference = &pipeline.InferenceExtension{Model: "qwen2.5:7b"}
	run(t, p, i, allow(200))
	ireq, _ := roleSplit(t, exp.GetSpans())
	checkAttr(t, ireq, "lineage.protocol", "inference")
	checkAttr(t, ireq, "inference.model", "qwen2.5:7b")
	if ireq.Name != "weather-service inference qwen2.5:7b" {
		t.Errorf("inference span name = %q", ireq.Name)
	}
}

// mcp-parser attaches to ANY JSON-RPC body — including every a2a exchange —
// so on an a2a hop both extensions are populated. The payload read is keyed by
// the protocol fact: when the a2a parser yields nothing (no text parts, a
// protocol-event artifact), the payload stays ABSENT rather than falling
// through to the co-populated MCP parse of the same bytes (which would emit
// the raw JSON-RPC envelope on an lineage.protocol=a2a span).
func TestCaptureIO_A2ANeverFallsThroughToCoPopulatedMCP(t *testing.T) {
	p, exp := newTestPlugin(t)
	p.cfg.CaptureIO = true
	pctx := fakeContext(pipeline.Outbound, http.Header{})
	pctx.Extensions.A2A = &pipeline.A2AExtension{
		Method: "message/send",
		// A status-update captured as the artifact — a protocol event, filtered.
		Artifact: `{"kind":"status-update","taskId":"t-1"}`,
	}
	pctx.Extensions.MCP = &pipeline.MCPExtension{
		Method: "message/send",
		Params: map[string]any{"message": map[string]any{"role": "user"}},
		Result: map[string]any{"artifacts": []any{map[string]any{"artifactId": "a-1"}}},
	}

	run(t, p, pctx, allow(200))
	req, resp := roleSplit(t, exp.GetSpans())

	checkAttr(t, req, "lineage.protocol", "a2a")
	if v, ok := findAttr(req, "input.value"); ok {
		t.Errorf("input.value = %q on an a2a hop with no a2a parts — leaked from the co-populated MCP parse", v.Emit())
	}
	if v, ok := findAttr(resp, "output.value"); ok {
		t.Errorf("output.value = %q on an a2a hop whose artifact is a protocol event — leaked from the co-populated MCP parse", v.Emit())
	}
	// mcp.* facts belong to mcp hops only; the a2a label must keep them off.
	if v, ok := findAttr(req, "mcp.method"); ok {
		t.Errorf("mcp.method = %q emitted on an a2a hop", v.Emit())
	}
}

func TestPrincipalFacts_InboundRequestOnly(t *testing.T) {
	p, exp := newTestPlugin(t)
	pctx := fakeContext(pipeline.Inbound, http.Header{})
	pctx.Identity = fakeIdentity{sub: "alice", client: "weather-ui", user: "Alice"}
	pctx.RemoteAddr = "10.244.2.5:47312"

	run(t, p, pctx, allow(200))
	req, resp := roleSplit(t, exp.GetSpans())

	// Inbound: the direct TCP caller IS the peer — the fact is emitted here.
	checkAttr(t, req, "lineage.peer.addr", "10.244.2.5:47312")
	checkAttr(t, req, "lineage.principal.sub", "alice")
	checkAttr(t, req, "lineage.principal.client", "weather-ui")
	// Principal facts are request-only.
	if _, ok := findAttr(resp, "lineage.principal.sub"); ok {
		t.Error("lineage.principal.sub leaked onto the response span")
	}
}

func TestPrincipalFacts_OutboundNeverEmitsPrincipal(t *testing.T) {
	p, exp := newTestPlugin(t)
	pctx := fakeContext(pipeline.Outbound, http.Header{})
	pctx.Identity = fakeIdentity{sub: "alice", client: "weather-ui"}

	run(t, p, pctx, allow(200))
	req, _ := roleSplit(t, exp.GetSpans())
	if _, ok := findAttr(req, "lineage.principal.sub"); ok {
		t.Error("outbound span carried a principal fact (inbound-only)")
	}
}

// ---- the forbidden-keys guard ----

// TestForbiddenKeysNeverEmitted scans every attribute of every span emitted
// across a spread of exchange shapes and asserts none carries a key from a
// removed vocabulary. The contract deleted these; this test is the tripwire
// that keeps them deleted.
func TestForbiddenKeysNeverEmitted(t *testing.T) {
	forbidden := []string{"trust.", "lineage.hop.kind", "enduser.id", "openinference.", "source", "authbridge.proxy"}

	shapes := []func() *pipeline.Context{
		func() *pipeline.Context {
			c := fakeContext(pipeline.Inbound, http.Header{})
			c.Identity = fakeIdentity{sub: "alice", client: "weather-ui", user: "Alice"}
			return c
		},
		func() *pipeline.Context {
			c := fakeContext(pipeline.Outbound, http.Header{})
			c.Extensions.MCP = &pipeline.MCPExtension{Method: "tools/call", Params: map[string]any{"name": "get_weather"}}
			return c
		},
		func() *pipeline.Context {
			c := fakeContext(pipeline.Outbound, http.Header{})
			c.Extensions.A2A = &pipeline.A2AExtension{Method: "message/send"}
			return c
		},
		func() *pipeline.Context {
			c := fakeContext(pipeline.Outbound, http.Header{})
			c.Extensions.Inference = &pipeline.InferenceExtension{Model: "qwen2.5:7b"}
			return c
		},
	}

	for _, mk := range shapes {
		p, exp := newTestPlugin(t)
		p.cfg.CaptureIO = true
		run(t, p, mk(), allow(200))
		for _, s := range exp.GetSpans() {
			for _, kv := range s.Attributes {
				key := string(kv.Key)
				for _, bad := range forbidden {
					if key == bad || strings.HasPrefix(key, bad) {
						t.Errorf("span %q emitted forbidden attribute %q", s.Name, key)
					}
				}
			}
		}
	}
}

// ---- robustness ----

func TestOnFinish_NoStateDoesNotPanicOrEmit(t *testing.T) {
	p, exp := newTestPlugin(t)
	pctx := fakeContext(pipeline.Inbound, http.Header{})
	// OnFinish without OnRequest having run — no exchangeState stored.
	p.OnFinish(context.Background(), pctx)
	if got := len(exp.GetSpans()); got != 0 {
		t.Errorf("OnFinish with no state emitted %d spans, want 0", got)
	}
}

func TestNotReady_SkipsSpan(t *testing.T) {
	p := NewLineageTelemetry()
	// Do NOT set ready — Init never called.
	pctx := fakeContext(pipeline.Inbound, http.Header{})
	action := p.OnRequest(context.Background(), pctx)
	if action.Type != pipeline.Continue {
		t.Fatalf("expected Continue, got %v", action.Type)
	}
	if pipeline.GetState[exchangeState](pctx, pluginName) != nil {
		t.Error("exchangeState should not be set when plugin is not ready")
	}
}

// ---- config ----

func TestConfigure_Defaults(t *testing.T) {
	p := NewLineageTelemetry()
	if err := p.Configure(nil); err != nil {
		t.Fatalf("Configure(nil): %v", err)
	}
	if p.cfg.OTelEndpoint != "localhost:4317" {
		t.Errorf("default endpoint = %q, want localhost:4317", p.cfg.OTelEndpoint)
	}
	if p.cfg.SelfIDFile != "/shared/client-id.txt" {
		t.Errorf("default self_id_file = %q", p.cfg.SelfIDFile)
	}
}

func TestConfigure_DecodesKeptKeys(t *testing.T) {
	p := NewLineageTelemetry()
	raw := json.RawMessage(`{"otel_endpoint":"http://collector:4317","capture_io":true,"self_id":"weather-service"}`)
	if err := p.Configure(raw); err != nil {
		t.Fatalf("Configure: %v", err)
	}
	if p.cfg.OTelEndpoint != "collector:4317" {
		t.Errorf("endpoint = %q, want collector:4317 (scheme stripped)", p.cfg.OTelEndpoint)
	}
	if !p.cfg.CaptureIO {
		t.Error("capture_io should be true")
	}
	if p.cfg.SelfID != "weather-service" {
		t.Errorf("self_id = %q", p.cfg.SelfID)
	}
}

// ---- helpers ----

type fakeIdentity struct {
	sub, client, user string
	scopes            []string
}

func (f fakeIdentity) Subject() string  { return f.sub }
func (f fakeIdentity) ClientID() string { return f.client }
func (f fakeIdentity) Scopes() []string { return f.scopes }
func (f fakeIdentity) Username() string { return f.user }

func findAttr(span tracetest.SpanStub, key string) (attribute.Value, bool) {
	for _, kv := range span.Attributes {
		if string(kv.Key) == key {
			return kv.Value, true
		}
	}
	return attribute.Value{}, false
}

func attrStr(span tracetest.SpanStub, key string) string {
	if v, ok := findAttr(span, key); ok {
		return v.AsString()
	}
	return ""
}

func intAttr(span tracetest.SpanStub, key string) (int64, bool) {
	if v, ok := findAttr(span, key); ok {
		return v.AsInt64(), true
	}
	return 0, false
}

// checkAttr asserts a span contains attribute key with the given string value.
func checkAttr(t *testing.T, span tracetest.SpanStub, key, want string) {
	t.Helper()
	got, ok := findAttr(span, key)
	if !ok {
		t.Errorf("attribute %q not found in span %q", key, span.Name)
		return
	}
	if got.AsString() != want {
		t.Errorf("attr %q = %q, want %q", key, got.AsString(), want)
	}
}

func headersEqual(a, b http.Header) bool {
	if len(a) != len(b) {
		return false
	}
	for k, av := range a {
		bv, ok := b[k]
		if !ok || len(av) != len(bv) {
			return false
		}
		for i := range av {
			if av[i] != bv[i] {
				return false
			}
		}
	}
	return true
}
