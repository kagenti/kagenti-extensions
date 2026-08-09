package extproc

import (
	"context"
	"testing"

	extprocv3 "github.com/envoyproxy/go-control-plane/envoy/service/ext_proc/v3"

	"github.com/rossoctl/cortex/authbridge/authlib/pipeline"
	"github.com/rossoctl/cortex/authbridge/authlib/plugins/plugintesting"
)

// hostCapture records the pctx.Host the listener built, so a test can assert
// what plugins actually see (Host is what SessionEvent.Host and the lineage
// plugin's lineage.peer.host fact are derived from).
type hostCapture struct {
	host string
}

func (p *hostCapture) Name() string { return "host-capture" }
func (p *hostCapture) Capabilities() pipeline.PluginCapabilities {
	return pipeline.PluginCapabilities{}
}
func (p *hostCapture) OnResponse(context.Context, *pipeline.Context) pipeline.Action {
	return pipeline.Action{Type: pipeline.Continue}
}

func (p *hostCapture) OnRequest(_ context.Context, pctx *pipeline.Context) pipeline.Action {
	p.host = pctx.Host
	return pipeline.Action{Type: pipeline.Continue}
}

func newHostCaptureServer(t *testing.T) (*Server, *hostCapture, *hostCapture) {
	t.Helper()
	in, out := &hostCapture{}, &hostCapture{}
	inbound, err := plugintesting.BuildPipeline([]pipeline.Plugin{in})
	if err != nil {
		t.Fatalf("building inbound pipeline: %v", err)
	}
	outbound, err := plugintesting.BuildPipeline([]pipeline.Plugin{out})
	if err != nil {
		t.Fatalf("building outbound pipeline: %v", err)
	}
	return &Server{
		InboundPipeline:  pipeline.NewHolder(inbound),
		OutboundPipeline: pipeline.NewHolder(outbound),
	}, in, out
}

func runOne(t *testing.T, srv *Server, req *extprocv3.ProcessingRequest) {
	t.Helper()
	_ = srv.Process(&mockStream{ctx: context.Background(), requests: []*extprocv3.ProcessingRequest{req}})
}

// TestExtProc_Authority asserts both directions carry the request authority on
// pctx.Host, from either the HTTP/2 pseudo-header or the HTTP/1 Host header.
// Inbound used to be left empty, which cost every inbound observation the
// address the workload was reached on.
func TestExtProc_Authority(t *testing.T) {
	cases := []struct {
		name     string
		inbound  bool
		headers  []string
		wantHost string
	}{
		{
			name:     "inbound from :authority",
			inbound:  true,
			headers:  []string{"x-authbridge-direction", "inbound", ":authority", "weather-service.team1.svc.cluster.local:8000", ":path", "/"},
			wantHost: "weather-service.team1.svc.cluster.local:8000",
		},
		{
			name:     "inbound falls back to the host header",
			inbound:  true,
			headers:  []string{"x-authbridge-direction", "inbound", "host", "weather-service:8000", ":path", "/"},
			wantHost: "weather-service:8000",
		},
		{
			name:     "outbound from :authority",
			headers:  []string{":authority", "weather-tool-mcp.team1.svc.cluster.local:8000", ":path", "/mcp"},
			wantHost: "weather-tool-mcp.team1.svc.cluster.local:8000",
		},
		{
			name:     "outbound falls back to the host header",
			headers:  []string{"host", "weather-tool-mcp:8000", ":path", "/mcp"},
			wantHost: "weather-tool-mcp:8000",
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			srv, in, out := newHostCaptureServer(t)
			headers := makeHeaders(tc.headers...)
			if tc.inbound {
				runOne(t, srv, inboundRequest(headers))
				if in.host != tc.wantHost {
					t.Errorf("inbound pctx.Host = %q; want %q", in.host, tc.wantHost)
				}
				return
			}
			runOne(t, srv, outboundRequest(headers))
			if out.host != tc.wantHost {
				t.Errorf("outbound pctx.Host = %q; want %q", out.host, tc.wantHost)
			}
		})
	}
}
