package extproc

import (
	"context"
	"testing"

	extprocv3 "github.com/envoyproxy/go-control-plane/envoy/service/ext_proc/v3"
	"github.com/rossoctl/cortex/authbridge/authlib/pipeline"
	"github.com/rossoctl/cortex/authbridge/authlib/plugins/plugintesting"
)

// pathCapture records the pctx.Path each pipeline run sees, so tests can
// assert on what the listener actually constructed rather than on side
// effects of a real plugin.
type pathCapture struct {
	paths []string
}

func (p *pathCapture) Name() string { return "path-capture" }
func (p *pathCapture) Capabilities() pipeline.PluginCapabilities {
	return pipeline.PluginCapabilities{}
}
func (p *pathCapture) OnRequest(_ context.Context, pctx *pipeline.Context) pipeline.Action {
	p.paths = append(p.paths, pctx.Path)
	return pipeline.Action{Type: pipeline.Continue}
}
func (p *pathCapture) OnResponse(_ context.Context, _ *pipeline.Context) pipeline.Action {
	return pipeline.Action{Type: pipeline.Continue}
}

func captureServer(t *testing.T) (*Server, *pathCapture, *pathCapture) {
	t.Helper()
	inCap, outCap := &pathCapture{}, &pathCapture{}
	inbound, err := plugintesting.BuildPipeline([]pipeline.Plugin{inCap})
	if err != nil {
		t.Fatalf("building inbound pipeline: %v", err)
	}
	outbound, err := plugintesting.BuildPipeline([]pipeline.Plugin{outCap})
	if err != nil {
		t.Fatalf("building outbound pipeline: %v", err)
	}
	return &Server{
		InboundPipeline:  pipeline.NewHolder(inbound),
		OutboundPipeline: pipeline.NewHolder(outbound),
	}, inCap, outCap
}

// The :path pseudo-header carries the full request target, query string
// included. pctx.Path must contain only the URL path, exactly as the
// forward and reverse proxy listeners produce it from r.URL.Path (query
// dropped, percent-decoding applied) — so plugin behavior keyed on Path
// cannot differ by listener mode.
func TestExtProc_PathMatchesProxyListeners(t *testing.T) {
	srv, inCap, outCap := captureServer(t)

	stream := &mockStream{
		ctx: context.Background(),
		requests: []*extprocv3.ProcessingRequest{
			inboundRequest(makeHeaders(
				"x-authbridge-direction", "inbound",
				":path", "/api/x?secret=1",
			)),
			outboundRequest(makeHeaders(
				":authority", "target-svc",
				":path", "/api/hello%20world?secret=1&b=2",
			)),
		},
	}
	_ = srv.Process(stream)

	if want := []string{"/api/x"}; len(inCap.paths) != 1 || inCap.paths[0] != want[0] {
		t.Errorf("inbound pctx.Path = %q, want %q", inCap.paths, want)
	}
	if want := []string{"/api/hello world"}; len(outCap.paths) != 1 || outCap.paths[0] != want[0] {
		t.Errorf("outbound pctx.Path = %q, want %q", outCap.paths, want)
	}
}
