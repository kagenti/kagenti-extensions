package extproc

import (
	"context"
	"fmt"
	"testing"

	extprocv3 "github.com/envoyproxy/go-control-plane/envoy/service/ext_proc/v3"

	"github.com/rossoctl/cortex/authbridge/authlib/auth"
	"github.com/rossoctl/cortex/authbridge/authlib/pipeline"
	"github.com/rossoctl/cortex/authbridge/authlib/plugins/plugintesting"
)

// traceRewriterPlugin mimics a pipeline plugin's trace-header writes — the
// lineage plugin's tracestate stamp today (wire contract v1.5), plus a
// traceparent rewrite to prove the diff mechanism covers it too. Used to
// assert the listener forwards trace-header rewrites as SetHeaders
// mutations: before headerDiffSetHeaders such rewrites died in
// pctx.Headers (inert on the wire — the phantom-root forests).
type traceRewriterPlugin struct {
	traceparent string
	tracestate  string
	readsBody   bool
}

func (p *traceRewriterPlugin) Name() string { return "trace-rewriter" }
func (p *traceRewriterPlugin) Capabilities() pipeline.PluginCapabilities {
	return pipeline.PluginCapabilities{ReadsBody: p.readsBody}
}
func (p *traceRewriterPlugin) OnRequest(_ context.Context, pctx *pipeline.Context) pipeline.Action {
	if p.traceparent != "" {
		pctx.Headers.Set("traceparent", p.traceparent)
	}
	if p.tracestate != "" {
		pctx.Headers.Set("tracestate", p.tracestate)
	}
	return pipeline.Action{Type: pipeline.Continue}
}
func (p *traceRewriterPlugin) OnResponse(_ context.Context, _ *pipeline.Context) pipeline.Action {
	return pipeline.Action{Type: pipeline.Continue}
}

func traceRewriterServer(t *testing.T, plugin pipeline.Plugin) *Server {
	t.Helper()
	outbound, err := pipeline.New([]pipeline.Plugin{plugin})
	if err != nil {
		t.Fatal(err)
	}
	inbound, err := plugintesting.BuildPipeline([]pipeline.Plugin{plugintesting.NewJWTValidation(auth.New(auth.Config{}), false)})
	if err != nil {
		t.Fatal(err)
	}
	return &Server{InboundPipeline: pipeline.NewHolder(inbound), OutboundPipeline: pipeline.NewHolder(outbound)}
}

// mutationHeaderValue returns the mutation value for key, or "" when absent.
// (setHeaderValue in placeholder_test.go unwraps a full RequestHeaders
// response; this one takes the bare mutation so body-phase responses can
// share it.)
func mutationHeaderValue(hm *extprocv3.HeaderMutation, key string) string {
	if hm == nil {
		return ""
	}
	for _, sh := range hm.SetHeaders {
		if sh.Header != nil && sh.Header.Key == key {
			return string(sh.Header.RawValue)
		}
	}
	return ""
}

// TestExtProc_Outbound_SpliceReachesWire: a plugin rewrite of the outbound
// traceparent/tracestate must be emitted as SetHeaders on the headers-phase
// response — this is what puts the lineage stamp on the wire.
func TestExtProc_Outbound_SpliceReachesWire(t *testing.T) {
	const newTP = "00-4bf92f3577b34da6a3ce929d0e0e4736-aaaaaaaaaaaaaaaa-01"
	const newTS = "dg-parent=aaaaaaaaaaaaaaaa"
	srv := traceRewriterServer(t, &traceRewriterPlugin{traceparent: newTP, tracestate: newTS})

	stream := &mockStream{
		ctx: context.Background(),
		requests: []*extprocv3.ProcessingRequest{
			outboundRequest(makeHeaders(
				":authority", "fanin-echo",
				"traceparent", "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
				"tracestate", "dg-parent=00f067aa0ba902b7",
			)),
		},
	}
	_ = srv.Process(stream)

	if len(stream.responses) != 1 {
		t.Fatalf("expected 1 response, got %d", len(stream.responses))
	}
	rh := stream.responses[0].GetRequestHeaders()
	if rh == nil || rh.Response == nil || rh.Response.HeaderMutation == nil {
		t.Fatalf("expected HeadersResponse with header mutation, got %+v", stream.responses[0])
	}
	if got := mutationHeaderValue(rh.Response.HeaderMutation, "traceparent"); got != newTP {
		t.Errorf("traceparent mutation = %q, want %q (splice inert on the wire)", got, newTP)
	}
	if got := mutationHeaderValue(rh.Response.HeaderMutation, "tracestate"); got != newTS {
		t.Errorf("tracestate mutation = %q, want %q", got, newTS)
	}
}

// TestExtProc_OutboundBody_SpliceReachesWire: same guarantee on the
// body-phase path (a ReadsBody plugin defers the pipeline to the body
// message; the trace-header diff must ride that response instead).
func TestExtProc_OutboundBody_SpliceReachesWire(t *testing.T) {
	const newTP = "00-4bf92f3577b34da6a3ce929d0e0e4736-bbbbbbbbbbbbbbbb-01"
	srv := traceRewriterServer(t, &traceRewriterPlugin{traceparent: newTP, readsBody: true})

	body := []byte(`{"jsonrpc":"2.0"}`)
	stream := &mockStream{
		ctx: context.Background(),
		requests: []*extprocv3.ProcessingRequest{
			outboundRequest(makeHeaders(
				":authority", "fanin-echo",
				"traceparent", "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
				"content-length", fmt.Sprintf("%d", len(body)),
			)),
			{Request: &extprocv3.ProcessingRequest_RequestBody{
				RequestBody: &extprocv3.HttpBody{Body: body},
			}},
		},
	}
	_ = srv.Process(stream)

	if len(stream.responses) != 2 {
		t.Fatalf("expected 2 responses, got %d", len(stream.responses))
	}
	rb := stream.responses[1].GetRequestBody()
	if rb == nil || rb.Response == nil || rb.Response.HeaderMutation == nil {
		t.Fatalf("expected RequestBody response with header mutation, got %+v", stream.responses[1])
	}
	if got := mutationHeaderValue(rb.Response.HeaderMutation, "traceparent"); got != newTP {
		t.Errorf("traceparent mutation = %q, want %q (splice inert on the body path)", got, newTP)
	}
}

// TestExtProc_Outbound_UnchangedTraceHeadersEmitNothing: when no plugin
// touches the trace headers, the listener must not emit mutations for them
// (echoing an unchanged header back would be a silent no-op today but
// masks diff regressions).
func TestExtProc_Outbound_UnchangedTraceHeadersEmitNothing(t *testing.T) {
	srv := traceRewriterServer(t, &traceRewriterPlugin{})

	stream := &mockStream{
		ctx: context.Background(),
		requests: []*extprocv3.ProcessingRequest{
			outboundRequest(makeHeaders(
				":authority", "fanin-echo",
				"traceparent", "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
				"tracestate", "dg-parent=00f067aa0ba902b7",
			)),
		},
	}
	_ = srv.Process(stream)

	if len(stream.responses) != 1 {
		t.Fatalf("expected 1 response, got %d", len(stream.responses))
	}
	rh := stream.responses[0].GetRequestHeaders()
	if rh == nil {
		t.Fatal("expected HeadersResponse")
	}
	if rh.Response != nil && rh.Response.HeaderMutation != nil {
		hm := rh.Response.HeaderMutation
		if v := mutationHeaderValue(hm, "traceparent"); v != "" {
			t.Errorf("unexpected traceparent mutation %q on unchanged header", v)
		}
		if v := mutationHeaderValue(hm, "tracestate"); v != "" {
			t.Errorf("unexpected tracestate mutation %q on unchanged header", v)
		}
	}
}
