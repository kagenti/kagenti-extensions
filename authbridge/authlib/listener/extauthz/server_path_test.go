package extauthz

import (
	"context"
	"testing"

	authv3 "github.com/envoyproxy/go-control-plane/envoy/service/auth/v3"

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

// Envoy's AttributeContext.HttpRequest.path is "the request target, as it
// appears in the first line of the HTTP request" — query string included.
// pctx.Path must contain only the URL path, exactly as the proxy listeners
// produce it from r.URL.Path (query dropped, percent-decoding applied), so
// plugin behavior keyed on Path cannot differ by listener mode. An
// unparseable target keeps the plain query-strip fallback.
func TestCheck_PathMatchesProxyListeners(t *testing.T) {
	cases := []struct {
		name   string
		target string
		want   string
	}{
		{"query stripped", "/api/x?secret=1", "/api/x"},
		{"percent-decoded", "/api/hello%20world?secret=1&b=2", "/api/hello world"},
		{"unparseable falls back to query strip", "/a%zz?secret=1", "/a%zz"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			inCap, outCap := &pathCapture{}, &pathCapture{}
			inbound, err := plugintesting.BuildPipeline([]pipeline.Plugin{inCap})
			if err != nil {
				t.Fatalf("building inbound pipeline: %v", err)
			}
			outbound, err := plugintesting.BuildPipeline([]pipeline.Plugin{outCap})
			if err != nil {
				t.Fatalf("building outbound pipeline: %v", err)
			}
			srv := &Server{
				InboundPipeline:  pipeline.NewHolder(inbound),
				OutboundPipeline: pipeline.NewHolder(outbound),
			}

			req := &authv3.CheckRequest{
				Attributes: &authv3.AttributeContext{
					Request: &authv3.AttributeContext_Request{
						Http: &authv3.AttributeContext_HttpRequest{
							Headers: map[string]string{":authority": "target-svc"},
							Path:    tc.target,
						},
					},
				},
			}
			if _, err := srv.Check(context.Background(), req); err != nil {
				t.Fatalf("Check: %v", err)
			}

			if len(inCap.paths) != 1 || inCap.paths[0] != tc.want {
				t.Errorf("inbound pctx.Path = %q, want [%q]", inCap.paths, tc.want)
			}
			if len(outCap.paths) != 1 || outCap.paths[0] != tc.want {
				t.Errorf("outbound pctx.Path = %q, want [%q]", outCap.paths, tc.want)
			}
		})
	}
}
