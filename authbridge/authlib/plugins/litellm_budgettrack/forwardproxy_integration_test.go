package litellm_budgettrack

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"path/filepath"
	"testing"
	"time"

	fwd "github.com/rossoctl/cortex/authbridge/authlib/listener/forwardproxy"
	"github.com/rossoctl/cortex/authbridge/authlib/pipeline"
	"github.com/rossoctl/cortex/authbridge/authlib/session"
)

// TestForwardProxyStreamedSSEUpdatesLedger is the listener-level test the PR #815
// review asked for: stand up the real forward proxy with a streamed
// (text/event-stream) upstream and BudgetTrack in the outbound pipeline, drive a
// request through the proxy, and assert the ledger moved.
//
// This exercises the path the direct-call unit tests structurally cannot: whether
// the listener actually dispatches response frames to the plugin. On the
// header-only version (before this branch), a streamed response never reaches the
// plugin's cost accounting, so this test would fail — which is exactly the gap the
// reviewer flagged.
func TestForwardProxyStreamedSSEUpdatesLedger(t *testing.T) {
	// Upstream emits Anthropic-style streamed usage and NO cost header — as
	// LiteLLM does for streamed responses — so the plugin must price it from the
	// parsed token usage.
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		w.WriteHeader(http.StatusOK)
		f, _ := w.(http.Flusher)
		io.WriteString(w, "event: message_start\ndata: {\"type\":\"message_start\",\"message\":{\"usage\":{\"input_tokens\":100,\"output_tokens\":1}}}\n\n")
		if f != nil {
			f.Flush()
		}
		io.WriteString(w, "event: message_delta\ndata: {\"type\":\"message_delta\",\"usage\":{\"output_tokens\":40}}\n\n")
		if f != nil {
			f.Flush()
		}
	}))
	t.Cleanup(upstream.Close)

	spend := filepath.Join(t.TempDir(), "spend.json")
	p := New()
	raw, _ := json.Marshal(budgetTrackConfig{
		SpendFile: spend, MaxBudget: 5, InputCostPerToken: 1e-6, OutputCostPerToken: 5e-6,
	})
	if err := p.Configure(raw); err != nil {
		t.Fatalf("Configure: %v", err)
	}
	// WrapConfigured is what the real build applies; it preserves StreamingResponder.
	wrapped := pipeline.WrapConfigured(p, raw)

	pipe, err := pipeline.New([]pipeline.Plugin{wrapped})
	if err != nil {
		t.Fatalf("pipeline.New: %v", err)
	}
	if !pipe.HasStreamingResponders() {
		t.Fatal("pipeline does not recognize BudgetTrack as a StreamingResponder")
	}
	store := session.New(5*time.Minute, 100, 0)
	t.Cleanup(store.Close)
	srv, err := fwd.NewServer(pipeline.NewHolder(pipe), store, nil)
	if err != nil {
		t.Fatalf("NewServer: %v", err)
	}
	proxy := httptest.NewServer(srv.Handler())
	t.Cleanup(proxy.Close)

	pu, _ := url.Parse(proxy.URL)
	client := &http.Client{Transport: &http.Transport{Proxy: http.ProxyURL(pu)}}
	req, err := http.NewRequestWithContext(t.Context(), http.MethodGet, upstream.URL+"/v1/messages", nil)
	if err != nil {
		t.Fatalf("new request: %v", err)
	}
	resp, err := client.Do(req)
	if err != nil {
		t.Fatalf("request via proxy: %v", err)
	}
	_, _ = io.Copy(io.Discard, resp.Body)
	if err := resp.Body.Close(); err != nil {
		t.Errorf("close body: %v", err)
	}

	// The terminal last=true dispatch runs in the handler's defer after the
	// stream is forwarded, so poll briefly for the ledger to settle.
	want := 100*1e-6 + 40*5e-6 // 0.0003
	for i := 0; i < 200; i++ {
		p.mu.Lock()
		got, calls := p.ledger.TotalSpend, p.ledger.TotalCalls
		p.mu.Unlock()
		if calls > 0 {
			if got < want-1e-12 || got > want+1e-12 {
				t.Fatalf("ledger TotalSpend = %v, want %v", got, want)
			}
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatal("ledger never updated from a streamed SSE response — the forward proxy did not dispatch frames to the plugin")
}
