package litellm_budgettrack

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"github.com/rossoctl/cortex/authbridge/authlib/pipeline"
)

// configure builds a BudgetTrack with a temp-dir spend file and the given budget.
func configure(t *testing.T, maxBudget float64) *BudgetTrack {
	t.Helper()
	p := New()
	cfg := budgetTrackConfig{
		SpendFile: filepath.Join(t.TempDir(), "spend.json"),
		MaxBudget: maxBudget,
	}
	raw, _ := json.Marshal(cfg)
	if err := p.Configure(raw); err != nil {
		t.Fatalf("Configure() error = %v", err)
	}
	return p
}

// TestOnResponseReadsResponseHeader is the regression guard for the core fix:
// the cost must be read from ResponseHeaders, not the request Headers.
func TestOnResponseReadsResponseHeader(t *testing.T) {
	p := configure(t, 5.00)
	pctx := &pipeline.Context{
		ResponseHeaders: http.Header{responseCostHeader: {"0.0025"}},
	}

	if action := p.OnResponse(context.Background(), pctx); action.Type != pipeline.Continue {
		t.Fatalf("OnResponse() = %v, want Continue", action.Type)
	}
	if p.ledger.TotalSpend != 0.0025 {
		t.Errorf("TotalSpend = %v, want 0.0025", p.ledger.TotalSpend)
	}
	if p.ledger.TotalCalls != 1 {
		t.Errorf("TotalCalls = %d, want 1", p.ledger.TotalCalls)
	}
}

// TestOnResponseIgnoresRequestHeader guards against the original bug: the cost
// header on the request side (pctx.Headers) must NOT be accumulated.
func TestOnResponseIgnoresRequestHeader(t *testing.T) {
	p := configure(t, 5.00)
	pctx := &pipeline.Context{
		Headers:         http.Header{responseCostHeader: {"0.0025"}}, // wrong place; must be ignored
		ResponseHeaders: http.Header{},
	}

	p.OnResponse(context.Background(), pctx)
	if p.ledger.TotalSpend != 0 {
		t.Errorf("TotalSpend = %v, want 0 (request-header cost must be ignored)", p.ledger.TotalSpend)
	}
}

// TestOnResponseFallsBackToOriginal covers the Anthropic /v1/messages case where
// only the pre-discount "-original" header is present.
func TestOnResponseFallsBackToOriginal(t *testing.T) {
	p := configure(t, 5.00)
	pctx := &pipeline.Context{
		ResponseHeaders: http.Header{responseCostOriginalHeader: {"2.204e-05"}},
	}

	p.OnResponse(context.Background(), pctx)
	if p.ledger.TotalSpend != 2.204e-05 {
		t.Errorf("TotalSpend = %v, want 2.204e-05 (fallback header)", p.ledger.TotalSpend)
	}
}

// TestOnResponseBareHeaderWins verifies the effective (post-discount) header
// takes precedence over "-original" when both are present.
func TestOnResponseBareHeaderWins(t *testing.T) {
	p := configure(t, 5.00)
	pctx := &pipeline.Context{
		ResponseHeaders: http.Header{
			responseCostHeader:         {"0.001"},
			responseCostOriginalHeader: {"0.002"},
		},
	}

	p.OnResponse(context.Background(), pctx)
	if p.ledger.TotalSpend != 0.001 {
		t.Errorf("TotalSpend = %v, want 0.001 (bare header must win)", p.ledger.TotalSpend)
	}
}

// TestOnResponseIgnoresMissingOrInvalid verifies absent / non-positive / unparseable
// costs are skipped rather than corrupting the ledger.
func TestOnResponseIgnoresMissingOrInvalid(t *testing.T) {
	for _, tc := range []struct {
		name    string
		headers http.Header
	}{
		{"missing", http.Header{}},
		{"zero", http.Header{responseCostHeader: {"0"}}},
		{"negative", http.Header{responseCostHeader: {"-1"}}},
		{"unparseable", http.Header{responseCostHeader: {"abc"}}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			p := configure(t, 5.00)
			pctx := &pipeline.Context{ResponseHeaders: tc.headers}
			if action := p.OnResponse(context.Background(), pctx); action.Type != pipeline.Continue {
				t.Fatalf("OnResponse() = %v, want Continue", action.Type)
			}
			if p.ledger.TotalSpend != 0 || p.ledger.TotalCalls != 0 {
				t.Errorf("ledger mutated: spend=%v calls=%d, want 0/0", p.ledger.TotalSpend, p.ledger.TotalCalls)
			}
		})
	}
}

// TestOnRequestEnforcesBudget verifies OnRequest denies with 429 once the
// accumulated spend reaches the daily budget, and allows before that.
func TestOnRequestEnforcesBudget(t *testing.T) {
	p := configure(t, 0.001)

	// Under budget: allowed.
	if action := p.OnRequest(context.Background(), &pipeline.Context{}); action.Type != pipeline.Continue {
		t.Fatalf("OnRequest() under budget = %v, want Continue", action.Type)
	}

	// Accumulate past the budget via a response.
	p.OnResponse(context.Background(), &pipeline.Context{
		ResponseHeaders: http.Header{responseCostHeader: {"0.002"}},
	})

	// Over budget: rejected with 429 / budget.exceeded.
	action := p.OnRequest(context.Background(), &pipeline.Context{})
	if action.Type != pipeline.Reject {
		t.Fatalf("OnRequest() over budget = %v, want Reject", action.Type)
	}
	// Stop before dereferencing: a nil Violation must not panic the next lines.
	if action.Violation == nil {
		t.Fatal("Violation is nil, want 429 budget.exceeded")
	}
	if action.Violation.Status != http.StatusTooManyRequests {
		t.Errorf("Violation.Status = %d, want 429", action.Violation.Status)
	}
	if action.Violation.Code != "budget.exceeded" {
		t.Errorf("Violation.Code = %q, want budget.exceeded", action.Violation.Code)
	}
}

// TestLedgerPersistsAcrossInstances verifies the spend file is reloaded, so a
// restart on the same day resumes the accumulated total.
func TestLedgerPersistsAcrossInstances(t *testing.T) {
	spendFile := filepath.Join(t.TempDir(), "spend.json")
	raw, _ := json.Marshal(budgetTrackConfig{SpendFile: spendFile, MaxBudget: 5.00})

	p1 := New()
	if err := p1.Configure(raw); err != nil {
		t.Fatalf("Configure() error = %v", err)
	}
	p1.OnResponse(context.Background(), &pipeline.Context{
		ResponseHeaders: http.Header{responseCostHeader: {"0.01"}},
	})

	p2 := New()
	if err := p2.Configure(raw); err != nil {
		t.Fatalf("Configure() error = %v", err)
	}
	if p2.ledger.TotalSpend != 0.01 {
		t.Errorf("reloaded TotalSpend = %v, want 0.01", p2.ledger.TotalSpend)
	}
}

// TestConfigureRejectsBadConfig verifies required-field and JSON validation.
func TestConfigureRejectsBadConfig(t *testing.T) {
	spend := filepath.Join(t.TempDir(), "spend.json")
	for _, tc := range []struct {
		name string
		raw  string
	}{
		{"empty spend_file", `{"max_budget": 5.0}`},
		{"zero max_budget", fmt.Sprintf(`{"spend_file": %q, "max_budget": 0}`, spend)},
		{"negative max_budget", fmt.Sprintf(`{"spend_file": %q, "max_budget": -1}`, spend)},
		{"negative input rate", fmt.Sprintf(`{"spend_file": %q, "max_budget": 5, "input_cost_per_token": -0.001}`, spend)},
		{"negative output rate", fmt.Sprintf(`{"spend_file": %q, "max_budget": 5, "output_cost_per_token": -0.001}`, spend)},
		{"negative cache write rate", fmt.Sprintf(`{"spend_file": %q, "max_budget": 5, "cache_write_cost_per_token": -0.001}`, spend)},
		{"negative cache read rate", fmt.Sprintf(`{"spend_file": %q, "max_budget": 5, "cache_read_cost_per_token": -0.001}`, spend)},
		{"invalid json", `{`},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if err := New().Configure(json.RawMessage(tc.raw)); err == nil {
				t.Errorf("Configure(%s) = nil, want error", tc.raw)
			}
		})
	}
}

// TestLoadLedgerResetsStaleDay verifies a spend file left over from a previous
// day is discarded on Configure rather than counted against today's budget.
func TestLoadLedgerResetsStaleDay(t *testing.T) {
	spend := filepath.Join(t.TempDir(), "spend.json")
	stale := `{"date":"2000-01-01","total_spend":9.99,"total_calls":42}`
	if err := os.WriteFile(spend, []byte(stale), 0o644); err != nil {
		t.Fatalf("seed spend file: %v", err)
	}

	p := New()
	raw, _ := json.Marshal(budgetTrackConfig{SpendFile: spend, MaxBudget: 5.00})
	if err := p.Configure(raw); err != nil {
		t.Fatalf("Configure() error = %v", err)
	}

	today := time.Now().UTC().Format("2006-01-02")
	if p.ledger.Date != today {
		t.Errorf("ledger.Date = %q, want %q", p.ledger.Date, today)
	}
	if p.ledger.TotalSpend != 0 || p.ledger.TotalCalls != 0 {
		t.Errorf("stale ledger not reset: spend=%v calls=%d", p.ledger.TotalSpend, p.ledger.TotalCalls)
	}

	// A same-day ledger, by contrast, is preserved.
	sameDay := fmt.Sprintf(`{"date":%q,"total_spend":1.25,"total_calls":3}`, today)
	if err := os.WriteFile(spend, []byte(sameDay), 0o644); err != nil {
		t.Fatalf("seed same-day file: %v", err)
	}
	p2 := New()
	if err := p2.Configure(raw); err != nil {
		t.Fatalf("Configure() error = %v", err)
	}
	if p2.ledger.TotalSpend != 1.25 || p2.ledger.TotalCalls != 3 {
		t.Errorf("same-day ledger not preserved: spend=%v calls=%d", p2.ledger.TotalSpend, p2.ledger.TotalCalls)
	}
}

// TestConcurrentOnResponse exercises the mutex under concurrent responses.
// Run with -race to catch data races on the ledger.
func TestConcurrentOnResponse(t *testing.T) {
	p := configure(t, 1000.0) // high budget so nothing is rejected
	const goroutines = 50

	var wg sync.WaitGroup
	wg.Add(goroutines)
	for i := 0; i < goroutines; i++ {
		go func() {
			defer wg.Done()
			p.OnResponse(context.Background(), &pipeline.Context{
				ResponseHeaders: http.Header{responseCostHeader: {"0.01"}},
			})
		}()
	}
	wg.Wait()

	if p.ledger.TotalCalls != goroutines {
		t.Errorf("TotalCalls = %d, want %d", p.ledger.TotalCalls, goroutines)
	}
	// 50 × 0.01 = 0.50, within float tolerance.
	if got := p.ledger.TotalSpend; got < 0.4999 || got > 0.5001 {
		t.Errorf("TotalSpend = %v, want ~0.50", got)
	}
}

// --- streaming (SSE usage) tests ---

// configurePriced builds a plugin with per-token streaming prices set.
func configurePriced(t *testing.T, maxBudget, inPer, outPer float64) *BudgetTrack {
	t.Helper()
	p := New()
	raw, _ := json.Marshal(budgetTrackConfig{
		SpendFile:          filepath.Join(t.TempDir(), "spend.json"),
		MaxBudget:          maxBudget,
		InputCostPerToken:  inPer,
		OutputCostPerToken: outPer,
	})
	if err := p.Configure(raw); err != nil {
		t.Fatalf("Configure() error = %v", err)
	}
	return p
}

// Anthropic-style streamed /v1/messages frames: input in message_start,
// cumulative output in message_delta, both in message_stop.
const (
	frameMessageStart = "event: message_start\n" +
		`data: {"type":"message_start","message":{"usage":{"input_tokens":100,"output_tokens":1}}}` + "\n"
	frameContentDelta = "event: content_block_delta\n" +
		`data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"hi"}}` + "\n"
	frameMessageDelta = "event: message_delta\n" +
		`data: {"type":"message_delta","usage":{"input_tokens":0,"output_tokens":40}}` + "\n"
)

// TestStreamingPricesFromUsage: header cost is absent/0 (streaming), so cost is
// computed from the parsed usage and the configured per-token rates.
func TestStreamingPricesFromUsage(t *testing.T) {
	p := configurePriced(t, 5.00, 1e-6, 5e-6) // $1/1M in, $5/1M out
	ctx := context.Background()
	pctx := &pipeline.Context{ResponseHeaders: http.Header{}} // streamed: no cost header

	p.OnResponseFrame(ctx, pctx, []byte(frameMessageStart), false)
	p.OnResponseFrame(ctx, pctx, []byte(frameContentDelta), false)
	p.OnResponseFrame(ctx, pctx, []byte(frameMessageDelta), false)
	p.OnResponseFrame(ctx, pctx, nil, true) // terminal frame settles cost

	want := 100*1e-6 + 40*5e-6 // 0.0001 + 0.0002 = 0.0003
	if got := p.ledger.TotalSpend; got < want-1e-12 || got > want+1e-12 {
		t.Errorf("TotalSpend = %v, want %v", got, want)
	}
	if p.ledger.TotalCalls != 1 {
		t.Errorf("TotalCalls = %d, want 1", p.ledger.TotalCalls)
	}
}

// TestStreamingWithoutPricesRecordsZero: no per-token rates configured -> a
// streamed response cannot be priced and must not corrupt the ledger.
func TestStreamingWithoutPricesRecordsZero(t *testing.T) {
	p := configure(t, 5.00) // no prices
	ctx := context.Background()
	pctx := &pipeline.Context{ResponseHeaders: http.Header{}}
	p.OnResponseFrame(ctx, pctx, []byte(frameMessageStart), false)
	p.OnResponseFrame(ctx, pctx, []byte(frameMessageDelta), false)
	p.OnResponseFrame(ctx, pctx, nil, true)
	if p.ledger.TotalSpend != 0 || p.ledger.TotalCalls != 0 {
		t.Errorf("ledger mutated without prices: spend=%v calls=%d", p.ledger.TotalSpend, p.ledger.TotalCalls)
	}
}

// TestHeaderCostWinsOverUsage: when the terminal frame has a real header cost
// (non-streaming buffered path delivered as a single frame), it is used and the
// per-token pricing is ignored.
func TestHeaderCostWinsOverUsage(t *testing.T) {
	p := configurePriced(t, 5.00, 1e-6, 5e-6)
	pctx := &pipeline.Context{ResponseHeaders: http.Header{responseCostHeader: {"0.02"}}}
	// single-frame buffered json also carries usage, which must be ignored
	body := []byte(`data: {"usage":{"prompt_tokens":100,"completion_tokens":40}}`)
	p.OnResponseFrame(context.Background(), pctx, body, true)
	if got := p.ledger.TotalSpend; got != 0.02 {
		t.Errorf("TotalSpend = %v, want 0.02 (header cost must win)", got)
	}
}

// TestOnResponseFrameOriginalFallback: streamed header 0 but non-streaming
// -original present on the terminal frame is still honored.
func TestOnResponseFrameOriginalFallback(t *testing.T) {
	p := configurePriced(t, 5.00, 1e-6, 5e-6)
	pctx := &pipeline.Context{ResponseHeaders: http.Header{responseCostOriginalHeader: {"6.688e-05"}}}
	p.OnResponseFrame(context.Background(), pctx, nil, true)
	if got := p.ledger.TotalSpend; got < 6.687e-05 || got > 6.689e-05 {
		t.Errorf("TotalSpend = %v, want 6.688e-05", got)
	}
}

// TestParseFrameUsageOpenAI covers the OpenAI terminal usage chunk shape.
func TestParseFrameUsageOpenAI(t *testing.T) {
	frame := []byte(`data: {"choices":[],"usage":{"prompt_tokens":30,"completion_tokens":12}}`)
	fu, ok := parseFrameUsage(frame)
	if !ok || fu.uncached != 30 || fu.output != 12 || fu.cacheWrite != 0 || fu.cacheRead != 0 {
		t.Errorf("parseFrameUsage = %+v (found=%v), want uncached 30 / output 12", fu, ok)
	}
}

// TestStreamingBareFrames reflects reality: the sseframe reader strips the
// "data:" prefix, so OnResponseFrame receives bare JSON payloads.
func TestStreamingBareFrames(t *testing.T) {
	p := configurePriced(t, 5.00, 1e-6, 5e-6)
	ctx := context.Background()
	pctx := &pipeline.Context{ResponseHeaders: http.Header{}}
	// bare-JSON frames (no "data:" prefix), as ReadFrame returns them
	p.OnResponseFrame(ctx, pctx, []byte(`{"type":"message_start","message":{"usage":{"input_tokens":100,"output_tokens":1}}}`), false)
	p.OnResponseFrame(ctx, pctx, []byte(`{"type":"message_delta","usage":{"output_tokens":40}}`), false)
	p.OnResponseFrame(ctx, pctx, nil, true)
	want := 100*1e-6 + 40*5e-6
	if got := p.ledger.TotalSpend; got < want-1e-12 || got > want+1e-12 {
		t.Errorf("TotalSpend = %v, want %v (bare-JSON frames)", got, want)
	}
}

// TestParseFrameUsageBareJSON unit-checks the bare-payload path directly.
func TestParseFrameUsageBareJSON(t *testing.T) {
	fu, ok := parseFrameUsage([]byte(`{"type":"message_delta","usage":{"input_tokens":14,"output_tokens":8}}`))
	if !ok || fu.uncached != 14 || fu.output != 8 {
		t.Errorf("parseFrameUsage(bare) = %+v (found=%v), want uncached 14 / output 8", fu, ok)
	}
}

// TestCacheTierParsing verifies the three input tiers are parsed separately.
func TestCacheTierParsing(t *testing.T) {
	fu, ok := parseFrameUsage([]byte(`{"usage":{"input_tokens":9,"cache_creation_input_tokens":3755,"cache_read_input_tokens":30008,"output_tokens":100}}`))
	if !ok || fu.uncached != 9 || fu.cacheWrite != 3755 || fu.cacheRead != 30008 || fu.output != 100 {
		t.Errorf("parseFrameUsage = %+v, want uncached 9 / cacheWrite 3755 / cacheRead 30008 / output 100", fu)
	}
}

// TestCacheTierPricing is the PR #816 must-fix: cache tiers must be priced
// separately, not flat at input_cost_per_token. Uses the real Claude Code turn
// from cortex#811 (input 9, cache_creation 3755, cache_read 30008).
func TestCacheTierPricing(t *testing.T) {
	p := New()
	raw, _ := json.Marshal(budgetTrackConfig{
		SpendFile:              filepath.Join(t.TempDir(), "spend.json"),
		MaxBudget:              100,
		InputCostPerToken:      1e-6,
		OutputCostPerToken:     5e-6,
		CacheWriteCostPerToken: 1.25e-6, // write premium
		CacheReadCostPerToken:  0.1e-6,  // read discount
	})
	if err := p.Configure(raw); err != nil {
		t.Fatal(err)
	}
	pctx := &pipeline.Context{ResponseHeaders: http.Header{"Content-Type": {"text/event-stream"}}}
	p.OnResponseFrame(context.Background(), pctx,
		[]byte(`{"usage":{"input_tokens":9,"cache_creation_input_tokens":3755,"cache_read_input_tokens":30008,"output_tokens":100}}`), false)
	p.OnResponseFrame(context.Background(), pctx, nil, true)

	want := 9*1e-6 + 3755*1.25e-6 + 30008*0.1e-6 + 100*5e-6
	if got := p.ledger.TotalSpend; got < want-1e-12 || got > want+1e-12 {
		t.Errorf("TotalSpend = %v, want %v (per-tier pricing)", got, want)
	}
	// Guard against a regression to flat pricing: flat would be far higher.
	flat := (9+3755+30008)*1e-6 + 100*5e-6
	if p.ledger.TotalSpend >= flat {
		t.Errorf("priced flat (%v) — cache tiers not applied", flat)
	}
}

// TestCacheRatesDefaultToInputRate: with cache rates unset, cached tokens are
// priced at the input rate (backward-compatible with pre-#816 flat behavior).
func TestCacheRatesDefaultToInputRate(t *testing.T) {
	p := configurePriced(t, 100, 1e-6, 5e-6) // no cache rates
	pctx := &pipeline.Context{ResponseHeaders: http.Header{"Content-Type": {"text/event-stream"}}}
	p.OnResponseFrame(context.Background(), pctx,
		[]byte(`{"usage":{"input_tokens":10,"cache_creation_input_tokens":20,"cache_read_input_tokens":30,"output_tokens":40}}`), false)
	p.OnResponseFrame(context.Background(), pctx, nil, true)
	want := (10+20+30)*1e-6 + 40*5e-6 // all input tiers at input rate
	if got := p.ledger.TotalSpend; got < want-1e-12 || got > want+1e-12 {
		t.Errorf("TotalSpend = %v, want %v (cache rates default to input rate)", got, want)
	}
}

// TestNonFiniteCostRejected guards the data-integrity fix from PR #815 review:
// strconv.ParseFloat accepts "NaN"/"Inf", both slip past a bare `cost <= 0`
// check, poison TotalSpend, and break the JSON marshal. The ledger must stay
// clean and its file must not be overwritten with garbage.
func TestNonFiniteCostRejected(t *testing.T) {
	for _, hdr := range []string{"NaN", "Inf", "+Inf", "-Inf"} {
		t.Run(hdr, func(t *testing.T) {
			p := configure(t, 5.00)
			p.OnResponse(context.Background(), &pipeline.Context{
				ResponseHeaders: http.Header{responseCostHeader: {hdr}},
			})
			if p.ledger.TotalSpend != 0 || p.ledger.TotalCalls != 0 {
				t.Errorf("%s: ledger mutated: spend=%v calls=%d", hdr, p.ledger.TotalSpend, p.ledger.TotalCalls)
			}
			// The spend file must remain valid JSON (not overwritten with garbage).
			if data, err := os.ReadFile(p.cfg.SpendFile); err == nil && len(data) > 0 {
				var l spendLedger
				if json.Unmarshal(data, &l) != nil {
					t.Errorf("%s: spend file corrupted: %s", hdr, data)
				}
			}
		})
	}
}

// TestCapabilitiesDeclaresReadsBody is the must-fix from PR #816 review: the
// plugin parses the response body, so it must declare ReadsBody or the extproc
// listener won't buffer the body (streamed accounting records nothing).
func TestCapabilitiesDeclaresReadsBody(t *testing.T) {
	if !New().Capabilities().ReadsBody {
		t.Error("Capabilities().ReadsBody = false; extproc will not buffer the body and streamed cost is lost")
	}
}

// TestOnResponseFrameSettlesOnce guards the exactly-once contract: a second
// terminal dispatch (e.g. extproc header + buffered-body phases) must not
// double-charge the ledger.
func TestOnResponseFrameSettlesOnce(t *testing.T) {
	p := configurePriced(t, 5.00, 1e-6, 5e-6)
	ctx := context.Background()
	pctx := &pipeline.Context{ResponseHeaders: http.Header{}}
	p.OnResponseFrame(ctx, pctx, []byte(`{"type":"message_start","message":{"usage":{"input_tokens":100,"output_tokens":1}}}`), false)
	p.OnResponseFrame(ctx, pctx, []byte(`{"type":"message_delta","usage":{"output_tokens":40}}`), false)
	p.OnResponseFrame(ctx, pctx, nil, true) // first terminal — charges
	p.OnResponseFrame(ctx, pctx, nil, true) // second terminal — must be a no-op
	want := 100*1e-6 + 40*5e-6
	if p.ledger.TotalCalls != 1 {
		t.Errorf("TotalCalls = %d, want 1 (double terminal dispatch must not double-charge)", p.ledger.TotalCalls)
	}
	if got := p.ledger.TotalSpend; got < want-1e-12 || got > want+1e-12 {
		t.Errorf("TotalSpend = %v, want %v", got, want)
	}
}

// TestZeroCostHeaderNonStreamedNotRepriced: a genuine free call (cost header
// "0", non-streamed) must be charged 0, not re-priced from its usage block.
func TestZeroCostHeaderNonStreamedNotRepriced(t *testing.T) {
	p := configurePriced(t, 5.00, 1e-6, 5e-6)
	pctx := &pipeline.Context{ResponseHeaders: http.Header{
		responseCostHeader: {"0"},
		"Content-Type":     {"application/json"},
	}}
	p.OnResponseFrame(context.Background(), pctx, []byte(`{"usage":{"input_tokens":100,"output_tokens":40}}`), true)
	if p.ledger.TotalSpend != 0 || p.ledger.TotalCalls != 0 {
		t.Errorf("free non-streamed call re-priced from usage: spend=%v calls=%d", p.ledger.TotalSpend, p.ledger.TotalCalls)
	}
}

// TestZeroCostHeaderStreamedPricesFromUsage: streamed responses always report a
// 0 cost header, so the usage fallback must still apply for text/event-stream.
func TestZeroCostHeaderStreamedPricesFromUsage(t *testing.T) {
	p := configurePriced(t, 5.00, 1e-6, 5e-6)
	pctx := &pipeline.Context{ResponseHeaders: http.Header{
		responseCostHeader: {"0"},
		"Content-Type":     {"text/event-stream; charset=utf-8"},
	}}
	p.OnResponseFrame(context.Background(), pctx, []byte(`{"type":"message_start","message":{"usage":{"input_tokens":100,"output_tokens":40}}}`), false)
	p.OnResponseFrame(context.Background(), pctx, nil, true)
	want := 100*1e-6 + 40*5e-6
	if got := p.ledger.TotalSpend; got < want-1e-12 || got > want+1e-12 {
		t.Errorf("streamed zero-header call not priced from usage: got %v want %v", got, want)
	}
}
