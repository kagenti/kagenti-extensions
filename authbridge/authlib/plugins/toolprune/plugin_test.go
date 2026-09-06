package toolprune

import (
	"context"
	"encoding/json"
	"fmt"
	"math"
	"strings"
	"sync"
	"testing"

	"github.com/rossoctl/cortex/authbridge/authlib/pipeline"
)

// anthropicBody is deliberately awkward: unsorted keys, odd indentation, a
// trailing field after tools. Byte-exactness assertions below depend on it
// staying awkward, because the whole safety claim is "every byte outside the
// deleted elements is unchanged".
const anthropicBody = `{"model":"claude-opus-5",
  "tools":[
    {"name":"Read","description":"read a file","input_schema":{"type":"object"}},
    {"name":"NotebookEdit","description":"edit a notebook","input_schema":{"type":"object"}},
    {"name":"Bash","description":"run a command","input_schema":{"type":"object"}}
  ],
  "max_tokens":1024,"stream":true}`

func configured(t *testing.T, remove ...string) *ToolPrune {
	t.Helper()
	p := New()
	raw, err := json.Marshal(map[string]any{"remove": remove})
	if err != nil {
		t.Fatal(err)
	}
	if err := p.Configure(raw); err != nil {
		t.Fatalf("Configure: %v", err)
	}
	return p
}

func inferenceCtx(path, body string, toolNames ...string) *pipeline.Context {
	pctx := &pipeline.Context{Path: path, Body: []byte(body)}
	tools := make([]pipeline.InferenceTool, 0, len(toolNames))
	for _, n := range toolNames {
		tools = append(tools, pipeline.InferenceTool{Name: n})
	}
	pctx.Extensions.Inference = &pipeline.InferenceExtension{Tools: tools}
	return pctx
}

func run(t *testing.T, p *ToolPrune, pctx *pipeline.Context, policies ...pipeline.ErrorPolicy) {
	t.Helper()
	var opts []pipeline.Option
	if len(policies) > 0 {
		opts = append(opts, pipeline.WithPolicies(policies...))
	}
	pipe, err := pipeline.New([]pipeline.Plugin{p}, opts...)
	if err != nil {
		t.Fatalf("pipeline.New: %v", err)
	}
	if act := pipe.Run(context.Background(), pctx); act.Type != pipeline.Continue {
		t.Fatalf("action = %v, want Continue — tool-prune must never block a request", act.Type)
	}
}

// TestPrune_LeavesEveryOtherByteIntact is the core safety claim. Deleting a
// tool must not reformat the document, reorder keys, or disturb whitespace: the
// request that reaches the model has to be the one the client sent, minus
// exactly the elements named.
func TestPrune_LeavesEveryOtherByteIntact(t *testing.T) {
	p := configured(t, "NotebookEdit")
	pctx := inferenceCtx("/v1/messages", anthropicBody, "Read", "NotebookEdit", "Bash")
	run(t, p, pctx)

	if !pctx.BodyMutated() {
		t.Fatal("expected the body to be rewritten")
	}
	got := string(pctx.Body)
	if strings.Contains(got, "NotebookEdit") {
		t.Errorf("removed tool still present:\n%s", got)
	}
	for _, keep := range []string{
		`"model":"claude-opus-5"`,
		`"name":"Read"`,
		`"name":"Bash"`,
		`"max_tokens":1024`,
		`"stream":true`,
	} {
		if !strings.Contains(got, keep) {
			t.Errorf("expected %s to survive verbatim:\n%s", keep, got)
		}
	}
	// The only difference from the original must be the removed element.
	if len(got) >= len(anthropicBody) {
		t.Errorf("body did not shrink: %d -> %d", len(anthropicBody), len(got))
	}
}

// TestPrune_DescendingDeletion: removing several tools by index only works if
// the deletions run high-to-low. An ascending loop would shift the array under
// itself and delete the wrong elements — here it would leave "Bash" and remove
// something else, so the assertion catches exactly that bug.
func TestPrune_DescendingDeletion(t *testing.T) {
	body := `{"tools":[{"name":"A"},{"name":"B"},{"name":"C"},{"name":"D"},{"name":"E"}]}`
	p := configured(t, "A", "B", "D")
	pctx := inferenceCtx("/v1/messages", body, "A", "B", "C", "D", "E")
	run(t, p, pctx)

	got := string(pctx.Body)
	for _, gone := range []string{`"A"`, `"B"`, `"D"`} {
		if strings.Contains(got, gone) {
			t.Errorf("tool %s should be gone: %s", gone, got)
		}
	}
	for _, kept := range []string{`"C"`, `"E"`} {
		if !strings.Contains(got, kept) {
			t.Errorf("tool %s should remain: %s", kept, got)
		}
	}
}

// TestPrune_OpenAIDialect: OpenAI nests the name under function, Anthropic puts
// it at the top level. Both must resolve, since the plugin reads names out of
// the raw bytes rather than trusting manifest ordering.
func TestPrune_OpenAIDialect(t *testing.T) {
	body := `{"tools":[{"type":"function","function":{"name":"Read"}},` +
		`{"type":"function","function":{"name":"NotebookEdit"}}]}`
	p := configured(t, "NotebookEdit")
	pctx := inferenceCtx("/v1/chat/completions", body, "Read", "NotebookEdit")
	run(t, p, pctx)

	got := string(pctx.Body)
	if strings.Contains(got, "NotebookEdit") {
		t.Errorf("removed tool still present: %s", got)
	}
	if !strings.Contains(got, "Read") {
		t.Errorf("kept tool missing: %s", got)
	}
}

// TestPrune_RemovingEveryToolDropsTheKeys: an empty tools array is not a safe
// output — OpenAI rejects `tools: []`, and tool_choice without tools. Drop both
// keys instead, so an over-broad remove list still yields a valid request.
func TestPrune_RemovingEveryToolDropsTheKeys(t *testing.T) {
	body := `{"model":"m","tools":[{"name":"A"},{"name":"B"}],"tool_choice":"auto"}`
	p := configured(t, "A", "B")
	pctx := inferenceCtx("/v1/chat/completions", body, "A", "B")
	run(t, p, pctx)

	got := string(pctx.Body)
	if strings.Contains(got, "tools") {
		t.Errorf("tools key should be gone entirely, not left empty: %s", got)
	}
	if strings.Contains(got, "tool_choice") {
		t.Errorf("tool_choice is invalid without tools; should be dropped: %s", got)
	}
	if !strings.Contains(got, `"model":"m"`) {
		t.Errorf("unrelated fields must survive: %s", got)
	}
}

// TestPrune_UnknownNamesIgnored: a name absent from this request's manifest is
// simply not there — not an error. Drift in the configured list costs savings,
// never correctness.
func TestPrune_UnknownNamesIgnored(t *testing.T) {
	body := `{"tools":[{"name":"Read"}]}`
	p := configured(t, "ToolThatDoesNotExist")
	pctx := inferenceCtx("/v1/messages", body, "Read")
	run(t, p, pctx)

	if pctx.BodyMutated() {
		t.Error("no configured tool was present; body must be untouched")
	}
	if string(pctx.Body) != body {
		t.Errorf("body = %s, want unchanged", pctx.Body)
	}
}

// TestPrune_FailsOpen: malformed, truncated and manifest-less bodies all
// forward the original bytes. A cost optimisation must never break a request.
func TestPrune_FailsOpen(t *testing.T) {
	cases := []struct {
		name string
		body string
	}{
		{"malformed json", `{"tools":[{"name":"NotebookEdit"}`},
		{"truncated mid-string", `{"tools":[{"name":"Notebook`},
		{"tools is not an array", `{"tools":"NotebookEdit"}`},
		{"tools absent", `{"model":"m"}`},
		{"empty body", ``},
		{"empty tools array", `{"tools":[]}`},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			p := configured(t, "NotebookEdit")
			pctx := inferenceCtx("/v1/messages", tc.body, "NotebookEdit")
			run(t, p, pctx)
			if pctx.BodyMutated() {
				t.Errorf("body was mutated; must fail open on %s", tc.name)
			}
			if string(pctx.Body) != tc.body {
				t.Errorf("body = %q, want original %q", pctx.Body, tc.body)
			}
		})
	}
}

// TestPrune_PathGate: only inference paths are touched, so an unrelated POST
// through the same proxy is never rewritten.
func TestPrune_PathGate(t *testing.T) {
	body := `{"tools":[{"name":"NotebookEdit"}]}`
	p := configured(t, "NotebookEdit")
	pctx := inferenceCtx("/v1/some/other/api", body, "NotebookEdit")
	run(t, p, pctx)
	if pctx.BodyMutated() {
		t.Error("non-inference path must not be pruned")
	}
}

// TestPrune_EmptyRemoveListIsNoop: the shipped default is an empty list, so the
// plugin must be inert until an operator fills it in.
func TestPrune_EmptyRemoveListIsNoop(t *testing.T) {
	p := configured(t)
	pctx := inferenceCtx("/v1/messages", anthropicBody, "Read", "NotebookEdit")
	run(t, p, pctx)
	if pctx.BodyMutated() {
		t.Error("empty remove list must not touch the body")
	}
	if p.Metrics() != nil {
		t.Errorf("no requests acted on; Metrics should be nil, got %+v", p.Metrics())
	}
}

// TestPrune_ObserveModeIsProjection: under on_error: observe the plugin computes
// exactly what it would remove and counts it, while the bytes on the wire stay
// untouched and the invocation is marked Shadow. That is what makes measure-only
// mode possible with one registration and no separate code path.
func TestPrune_ObserveModeIsProjection(t *testing.T) {
	p := configured(t, "NotebookEdit")
	pctx := inferenceCtx("/v1/messages", anthropicBody, "Read", "NotebookEdit", "Bash")
	run(t, p, pctx, pipeline.ErrorPolicyObserve)

	if pctx.BodyMutated() {
		t.Error("observe mode must leave the wire untouched")
	}
	if string(pctx.Body) != anthropicBody {
		t.Errorf("body changed under observe:\n%s", pctx.Body)
	}
	if pctx.Extensions.Invocations == nil {
		t.Fatal("expected invocations to be recorded")
	}
	var sawShadowModify bool
	for _, inv := range pctx.Extensions.Invocations.Inbound {
		if inv.Shadow && inv.Reason == "body_rewritten" {
			sawShadowModify = true
		}
	}
	if !sawShadowModify {
		t.Errorf("expected a Shadow=true body_rewritten invocation, got %+v",
			pctx.Extensions.Invocations.Inbound)
	}

	// The projection must still be countable, and must be reported as a
	// projection rather than a realised saving.
	if p.m.requestsProjected != 1 {
		t.Errorf("requestsProjected = %d, want 1", p.m.requestsProjected)
	}
	if p.m.requestsPruned != 0 {
		t.Errorf("requestsPruned = %d, want 0 under observe", p.m.requestsPruned)
	}
	if p.m.bytesRemoved == 0 {
		t.Error("bytesRemoved must accumulate under observe — that is the projection")
	}
	if !hasMetric(p.Metrics(), "requests projected") {
		t.Errorf("readout should say 'requests projected': %+v", p.Metrics())
	}
}

// TestPrune_EnforceCountsPruned is the enforce-mode counterpart.
func TestPrune_EnforceCountsPruned(t *testing.T) {
	p := configured(t, "NotebookEdit")
	pctx := inferenceCtx("/v1/messages", anthropicBody, "Read", "NotebookEdit", "Bash")
	run(t, p, pctx, pipeline.ErrorPolicyEnforce)

	if p.m.requestsPruned != 1 {
		t.Errorf("requestsPruned = %d, want 1", p.m.requestsPruned)
	}
	if p.m.requestsProjected != 0 {
		t.Errorf("requestsProjected = %d, want 0 under enforce", p.m.requestsProjected)
	}
	if p.m.toolsRemoved != 1 {
		t.Errorf("toolsRemoved = %d, want 1", p.m.toolsRemoved)
	}
	if !hasMetric(p.Metrics(), "removed: NotebookEdit") {
		t.Errorf("per-tool attribution missing: %+v", p.Metrics())
	}
}

// finish drives OnFinish with a given per-tier usage split.
func finish(t *testing.T, p *ToolPrune, pctx *pipeline.Context, input, cacheRead, cacheWrite int) {
	t.Helper()
	pctx.Extensions.Inference.InputTokens = input
	pctx.Extensions.Inference.CacheReadTokens = cacheRead
	pctx.Extensions.Inference.CacheWriteTokens = cacheWrite
	p.OnFinish(context.Background(), pctx)
}

func pruneOnce(t *testing.T, p *ToolPrune) *pipeline.Context {
	t.Helper()
	pctx := inferenceCtx("/v1/messages", anthropicBody, "Read", "NotebookEdit", "Bash")
	run(t, p, pctx)
	if !pctx.BodyMutated() {
		t.Fatal("expected a prune")
	}
	return pctx
}

// TestMetrics_NoUsageYetReportsZero: before any response usage is seen there is
// no ratio to convert bytes with, so report zero with the reason rather than a
// number or a NaN.
func TestMetrics_NoUsageYetReportsZero(t *testing.T) {
	p := configured(t, "NotebookEdit")
	pruneOnce(t, p)
	m := findMetric(t, p.Metrics(), "tokens saved")
	if m.Value != 0 || m.Note != "no response usage seen yet" {
		t.Errorf("got %+v, want 0 with the missing-sample reason", m)
	}
}

// TestMetrics_AttributesSavingToTheRightTier is the core of the design. The tool
// manifest lives in the cached prefix, so on a cache-miss request the saving
// comes out of cache writes and on a hit out of cache reads. Reporting one
// blended token count would hide which — and the tiers are priced up to 12x
// apart, so that distinction is the whole number.
func TestMetrics_AttributesSavingToTheRightTier(t *testing.T) {
	tests := []struct {
		name                         string
		input, cacheRead, cacheWrite int
		wantRow                      string
	}{
		{"cache miss writes the prefix", 8881, 0, 24701, "tokens saved: cache write"},
		{"cache hit reads the prefix", 26, 24701, 8907, "tokens saved: cache read"},
		{"no caching at all", 40000, 0, 0, "tokens saved: input"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			p := configured(t, "NotebookEdit")
			pctx := pruneOnce(t, p)
			finish(t, p, pctx, tc.input, tc.cacheRead, tc.cacheWrite)

			ms := p.Metrics()
			got := findMetric(t, ms, tc.wantRow)
			if got.Value <= 0 {
				t.Errorf("%s = %v, want positive", tc.wantRow, got.Value)
			}
			if !strings.HasPrefix(got.Note, "estimate, n=") {
				t.Errorf("note = %q, want it labelled an estimate with its sample", got.Note)
			}
			// No other tier may be credited, and there must be no blended total.
			for _, m := range ms {
				if m.Name == "tokens saved" {
					t.Error("a blended 'tokens saved' row invites multiplying by one rate")
				}
				if strings.HasPrefix(m.Name, "tokens saved: ") && m.Name != tc.wantRow {
					t.Errorf("saving also credited to %q", m.Name)
				}
			}
		})
	}
}

// TestPricing_DefaultsPriceKnownModelsWithoutConfig: the built-in table exists
// so a dollar figure appears with no configuration at all — the difference
// between a number an operator sees and one they never get around to enabling.
func TestPricing_DefaultsPriceKnownModelsWithoutConfig(t *testing.T) {
	p := configured(t, "NotebookEdit") // no pricing configured whatsoever
	pruneWithModel(t, p, "claude-opus-5")

	m := findMetric(t, p.Metrics(), "$ saved")
	if m.Value <= 0 {
		t.Errorf("$ saved = %v, want a figure from the built-in rates", m.Value)
	}
	// Provenance must travel with the number: built-in rates are
	// gateway-specific and never refreshed, so this must not read as measured.
	// The note must disclose three things: that the rates are built in, the
	// DIRECTION of the error (they came from a discounted gateway, so anyone on
	// vendor list is under-credited), and how to override. "default rates" alone
	// reads as a rounding caveat rather than a several-fold one.
	for _, want := range []string{"built-in rates", "understates", "pricing."} {
		if !strings.Contains(m.Note, want) {
			t.Errorf("note = %q, missing %q", m.Note, want)
		}
	}
}

// TestPricing_ConfigOverridesDefaults: an operator on a different gateway must
// be able to correct a model without the built-in value leaking through, and the
// note must stop claiming defaults were used.
func TestPricing_ConfigOverridesDefaults(t *testing.T) {
	base := configured(t, "NotebookEdit")
	pruneWithModel(t, base, "claude-opus-5")
	fromDefault := findMetric(t, base.Metrics(), "$ saved").Value

	// Ten times the built-in input rate.
	over := configuredJSON(t, `{"remove":["NotebookEdit"],
	  "pricing":{"claude-opus-5":{"input_cost_per_token":3.8e-05,"cache_write_cost_per_token":4.75e-05}}}`)
	pruneWithModel(t, over, "claude-opus-5")
	m := findMetric(t, over.Metrics(), "$ saved")

	if ratio := m.Value / fromDefault; ratio < 9.5 || ratio > 10.5 {
		t.Errorf("configured/default cost ratio = %.2f, want ~10 — config must win outright", ratio)
	}
	if strings.Contains(m.Note, "default rates") {
		t.Errorf("note = %q, must not claim defaults when the operator configured the model", m.Note)
	}
}

// TestPricing_UnknownModelStillUnpriced: the defaults cover a known set, not
// everything. A model in neither the table nor the config is counted, not
// charged at some other model's rate.
func TestPricing_UnknownModelStillUnpriced(t *testing.T) {
	p := configured(t, "NotebookEdit")
	pruneWithModel(t, p, "gcp/gemini-3-pro-preview")

	gap := findMetric(t, p.Metrics(), "requests unpriced")
	if gap.Value != 1 || !strings.Contains(gap.Note, "gemini") {
		t.Errorf("unpriced row = %+v, want 1 naming the model", gap)
	}
	for _, m := range p.Metrics() {
		if m.Name == "$ saved" {
			t.Errorf("$ saved = %v for a model with no rate anywhere, want no row", m.Value)
		}
	}
}

// TestMetrics_TierRatesDifferBy12x pins the reason the tiers are separate. The
// same pruned bytes, priced as a cache write versus a cache read at Anthropic's
// published ratios, differ by more than an order of magnitude. A flat rate would
// be wrong by that factor.
func TestMetrics_TierRatesDifferBy12x(t *testing.T) {
	cfg := func(t *testing.T) *ToolPrune {
		p := New()
		raw := []byte(`{"remove":["NotebookEdit"],` +
			`"input_cost_per_token":1e-05,` +
			`"cache_write_cost_per_token":1.25e-05,` + // 1.25x input
			`"cache_read_cost_per_token":1e-06}`) // 0.1x input
		if err := p.Configure(raw); err != nil {
			t.Fatal(err)
		}
		return p
	}

	write := cfg(t)
	finish(t, write, pruneOnce(t, write), 0, 0, 24701)
	read := cfg(t)
	finish(t, read, pruneOnce(t, read), 0, 24701, 0)

	w := findMetric(t, write.Metrics(), "$ saved").Value
	r := findMetric(t, read.Metrics(), "$ saved").Value
	if w <= 0 || r <= 0 {
		t.Fatalf("expected both priced: write=%v read=%v", w, r)
	}
	if ratio := w / r; ratio < 12 || ratio > 13 {
		t.Errorf("cache-write / cache-read cost ratio = %.2f, want ~12.5 (1.25x vs 0.1x input)", ratio)
	}
	// And a per-request figure alongside the total.
	if pr := findMetric(t, write.Metrics(), "$ saved / request"); pr.Value <= 0 {
		t.Errorf("$ saved / request = %v, want positive", pr.Value)
	}
}

// TestMetrics_ConcurrentAccess exercises Metrics() against live counter updates.
// describePipeline calls it from the HTTP handler while requests are in flight,
// so it must be safe under -race.
func TestMetrics_ConcurrentAccess(t *testing.T) {
	p := configured(t, "NotebookEdit")
	var wg sync.WaitGroup
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for j := 0; j < 50; j++ {
				pctx := inferenceCtx("/v1/messages", anthropicBody, "Read", "NotebookEdit")
				pipe, err := pipeline.New([]pipeline.Plugin{p})
				if err != nil {
					t.Error(err)
					return
				}
				pipe.Run(context.Background(), pctx)
			}
		}()
	}
	for i := 0; i < 4; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for j := 0; j < 100; j++ {
				_ = p.Metrics()
			}
		}()
	}
	wg.Wait()
	if p.m.requestsPruned != 8*50 {
		t.Errorf("requestsPruned = %d, want %d", p.m.requestsPruned, 8*50)
	}
}

func TestConfigure_RejectsUnknownFields(t *testing.T) {
	p := New()
	err := p.Configure(json.RawMessage(`{"remove":["A"],"nope":1}`))
	if err == nil {
		t.Fatal("expected an error for an unknown config field")
	}
	if !strings.Contains(err.Error(), "tool-prune config") {
		t.Errorf("error should name the plugin: %v", err)
	}
}

func TestCapabilities_RequestOnlySoStreamingSurvives(t *testing.T) {
	caps := New().Capabilities()
	if !caps.WritesRequestBody {
		t.Error("must declare WritesRequestBody")
	}
	if caps.WritesResponseBody {
		t.Error("must NOT declare WritesResponseBody — it would cost SSE streaming for nothing")
	}
	if len(caps.RequiresAny) != 1 || caps.RequiresAny[0] != "inference-parser" {
		t.Errorf("RequiresAny = %v, want [inference-parser]", caps.RequiresAny)
	}
}

func hasMetric(ms []pipeline.Metric, name string) bool {
	for _, m := range ms {
		if m.Name == name {
			return true
		}
	}
	return false
}

func findMetric(t *testing.T, ms []pipeline.Metric, name string) pipeline.Metric {
	t.Helper()
	for _, m := range ms {
		if m.Name == name {
			return m
		}
	}
	t.Fatalf("metric %q not found in %+v", name, ms)
	return pipeline.Metric{}
}

// TestPrune_NeverRemovesForcedToolChoice: a tool_choice that forces a specific
// tool must keep that tool, whichever dialect spells it. Removing it leaves a
// tool_choice naming a tool absent from the manifest, which providers reject —
// turning a cost optimisation into a 400, the one thing this plugin must never
// do. The rest of the remove list still applies.
func TestPrune_NeverRemovesForcedToolChoice(t *testing.T) {
	cases := []struct {
		name string
		body string
	}{
		{
			name: "anthropic tool_choice.name",
			body: `{"tools":[{"name":"Read"},{"name":"WebSearch"},{"name":"NotebookEdit"}],` +
				`"tool_choice":{"type":"tool","name":"WebSearch"}}`,
		},
		{
			name: "openai tool_choice.function.name",
			body: `{"tools":[{"type":"function","function":{"name":"Read"}},` +
				`{"type":"function","function":{"name":"WebSearch"}},` +
				`{"type":"function","function":{"name":"NotebookEdit"}}],` +
				`"tool_choice":{"type":"function","function":{"name":"WebSearch"}}}`,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			// Both WebSearch (forced) and NotebookEdit are configured for removal.
			p := configured(t, "WebSearch", "NotebookEdit")
			pctx := inferenceCtx("/v1/messages", tc.body, "Read", "WebSearch", "NotebookEdit")
			run(t, p, pctx)

			got := string(pctx.Body)
			if !strings.Contains(got, "WebSearch") {
				t.Errorf("forced tool was removed — request is now invalid:\n  %s", got)
			}
			if strings.Contains(got, "NotebookEdit") {
				t.Errorf("non-forced tool should still be pruned:\n  %s", got)
			}
		})
	}
}

// TestPrune_ToolChoiceAutoDoesNotBlockPruning: "auto" / "none" name no tool, so
// they must not be mistaken for a forced choice and suppress all pruning.
func TestPrune_ToolChoiceAutoDoesNotBlockPruning(t *testing.T) {
	for _, choice := range []string{`"auto"`, `"none"`, `{"type":"auto"}`} {
		t.Run(choice, func(t *testing.T) {
			body := `{"tools":[{"name":"Read"},{"name":"NotebookEdit"}],"tool_choice":` + choice + `}`
			p := configured(t, "NotebookEdit")
			pctx := inferenceCtx("/v1/messages", body, "Read", "NotebookEdit")
			run(t, p, pctx)
			if strings.Contains(string(pctx.Body), "NotebookEdit") {
				t.Errorf("tool_choice %s should not suppress pruning:\n  %s", choice, pctx.Body)
			}
		})
	}
}

// TestPrune_PathGateIgnoresQueryString: providers accept query parameters on
// these endpoints, and Claude Code really does send /v1/messages?beta=true. A
// suffix match against the raw target misses every such request and the plugin
// silently does nothing — the least debuggable possible failure, because
// everything looks configured correctly.
func TestPrune_PathGateIgnoresQueryString(t *testing.T) {
	body := `{"tools":[{"name":"Read"},{"name":"NotebookEdit"}]}`
	for _, path := range []string{
		"/v1/messages",
		"/v1/messages?beta=true",
		"/v1/messages?beta=true&x=1",
		"/v1/messages/",
		"/v1/chat/completions?stream=false",
		"https://host/v1/messages?beta=true", // absolute-form target via a proxy
	} {
		t.Run(path, func(t *testing.T) {
			p := configured(t, "NotebookEdit")
			pctx := inferenceCtx(path, body, "Read", "NotebookEdit")
			run(t, p, pctx)
			if !pctx.BodyMutated() {
				t.Errorf("path %q was not treated as an inference endpoint", path)
			}
		})
	}
}

// TestPrune_NonInferencePathsStillSkip guards the other direction: loosening the
// gate must not make it match everything.
func TestPrune_NonInferencePathsStillSkip(t *testing.T) {
	body := `{"tools":[{"name":"NotebookEdit"}]}`
	for _, path := range []string{"/mcp", "/v1/models", "/healthz", "/v1/messages/batches"} {
		t.Run(path, func(t *testing.T) {
			p := configured(t, "NotebookEdit")
			pctx := inferenceCtx(path, body, "NotebookEdit")
			run(t, p, pctx)
			if pctx.BodyMutated() {
				t.Errorf("path %q must not be pruned", path)
			}
		})
	}
}

// TestPrune_TunnelSkipIsDistinguishable: a CONNECT tunnel has no path. Reporting
// that as a path mismatch sent a real investigation hunting for a routing
// problem when the actual cause was that TLS was never decrypted. The reason
// code has to say which.
func TestPrune_TunnelSkipIsDistinguishable(t *testing.T) {
	p := configured(t, "NotebookEdit")
	pctx := inferenceCtx("", `{"tools":[{"name":"NotebookEdit"}]}`, "NotebookEdit")
	run(t, p, pctx)

	if pctx.Extensions.Invocations == nil || len(pctx.Extensions.Invocations.Inbound) == 0 {
		t.Fatal("expected a skip invocation")
	}
	inv := pctx.Extensions.Invocations.Inbound[0]
	if inv.Reason != "no_path_tunnelled" {
		t.Errorf("reason = %q, want no_path_tunnelled so a tunnel is not mistaken for a routing problem", inv.Reason)
	}
}

// TestPrune_PathMismatchRecordsThePath: a skip that does not say what it saw
// cannot be diagnosed from the session timeline.
func TestPrune_PathMismatchRecordsThePath(t *testing.T) {
	p := configured(t, "NotebookEdit")
	pctx := inferenceCtx("/v1/models", `{"tools":[{"name":"NotebookEdit"}]}`, "NotebookEdit")
	run(t, p, pctx)
	inv := pctx.Extensions.Invocations.Inbound[0]
	if inv.Reason != "path_not_inference" || inv.Path != "/v1/models" {
		t.Errorf("inv = %+v, want path_not_inference with the offending path recorded", inv)
	}
}

// configuredJSON builds a plugin from raw config JSON.
func configuredJSON(t *testing.T, raw string) *ToolPrune {
	t.Helper()
	p := New()
	if err := p.Configure(json.RawMessage(raw)); err != nil {
		t.Fatalf("Configure: %v", err)
	}
	return p
}

// pruneWithModel runs one prune and finishes it as the named model, with a
// cache-write split (the cache-miss shape).
func pruneWithModel(t *testing.T, p *ToolPrune, model string) {
	t.Helper()
	pctx := inferenceCtx("/v1/messages", anthropicBody, "Read", "NotebookEdit", "Bash")
	run(t, p, pctx)
	pctx.Extensions.Inference.Model = model
	pctx.Extensions.Inference.CacheWriteTokens = 24701
	p.OnFinish(context.Background(), pctx)
}

const perModelCfg = `{"remove":["NotebookEdit"],"pricing":{
  "claude-opus-5":       {"input_cost_per_token":1e-05,"cache_write_cost_per_token":1.25e-05,"cache_read_cost_per_token":1e-06},
  "aws/claude-sonnet-5": {"input_cost_per_token":4e-06,"cache_write_cost_per_token":5e-06,"cache_read_cost_per_token":4e-07},
  "aws/claude-haiku-4-5":{"input_cost_per_token":2e-06,"cache_write_cost_per_token":2.5e-06,"cache_read_cost_per_token":2e-07}}}`

// TestPricing_PerModelRatesDiffer is why pricing is keyed by model. Across the
// Claude family the input rate spans roughly 5x (opus 1.0x, sonnet ~0.4x, haiku
// ~0.2x). Charging every request at one rate would misstate the saving by that
// factor depending on which model happened to serve it. The rates below are
// synthetic, chosen to reproduce those ratios exactly.
func TestPricing_PerModelRatesDiffer(t *testing.T) {
	usd := map[string]float64{}
	for _, model := range []string{"claude-opus-5", "aws/claude-sonnet-5", "aws/claude-haiku-4-5"} {
		p := configuredJSON(t, perModelCfg)
		pruneWithModel(t, p, model)
		usd[model] = findMetric(t, p.Metrics(), "$ saved").Value
		if usd[model] <= 0 {
			t.Fatalf("%s: no cost reported", model)
		}
	}
	// Same saved bytes, same tier — cost must track the model's rate ratios.
	if r := usd["claude-opus-5"] / usd["aws/claude-sonnet-5"]; r < 2.4 || r > 2.6 {
		t.Errorf("opus/sonnet cost ratio = %.2f, want ~2.5", r)
	}
	if r := usd["claude-opus-5"] / usd["aws/claude-haiku-4-5"]; r < 4.9 || r > 5.1 {
		t.Errorf("opus/haiku cost ratio = %.2f, want ~5.0", r)
	}
}

// TestPricing_UnknownModelIsCountedNotGuessed: charging an unpriced model at
// another model's rate would be wrong by up to 5x, so it is reported as a gap.
func TestPricing_UnknownModelIsCountedNotGuessed(t *testing.T) {
	p := configuredJSON(t, perModelCfg)
	pruneWithModel(t, p, "gcp/gemini-3-pro-preview")

	ms := p.Metrics()
	gap := findMetric(t, ms, "requests unpriced")
	if gap.Value != 1 {
		t.Errorf("requests unpriced = %v, want 1", gap.Value)
	}
	if !strings.Contains(gap.Note, "gcp/gemini-3-pro-preview") {
		t.Errorf("note should name the unpriced model, got %q", gap.Note)
	}
	// Tokens are still counted — only the dollars are withheld.
	if findMetric(t, ms, "tokens saved: cache write").Value <= 0 {
		t.Error("token saving should still be reported for an unpriced model")
	}
	for _, m := range ms {
		if m.Name == "$ saved" && m.Value != 0 {
			t.Errorf("$ saved = %v for an unpriced model, want no charge", m.Value)
		}
	}
}

// TestPricing_FlatRatesActAsFallback keeps the simpler single-model config
// working: a model absent from the table is priced at the flat rates when set.
func TestPricing_FlatRatesActAsFallback(t *testing.T) {
	p := configuredJSON(t, `{"remove":["NotebookEdit"],"input_cost_per_token":1e-05,
	  "pricing":{"aws/claude-haiku-4-5":{"input_cost_per_token":2e-06}}}`)
	pruneWithModel(t, p, "some-other-model")
	if findMetric(t, p.Metrics(), "$ saved").Value <= 0 {
		t.Error("a model absent from pricing should fall back to the flat rates")
	}
	for _, m := range p.Metrics() {
		if m.Name == "requests unpriced" {
			t.Error("should not be counted unpriced when a fallback rate exists")
		}
	}
}

// TestPricing_ModelMatchIsCaseInsensitive: gateways vary in how they echo model
// names, and a case mismatch would silently unprice the traffic.
func TestPricing_ModelMatchIsCaseInsensitive(t *testing.T) {
	p := configuredJSON(t, `{"remove":["NotebookEdit"],"pricing":{"Claude-Opus-5":{"input_cost_per_token":1e-05}}}`)
	pruneWithModel(t, p, "claude-opus-5")
	if findMetric(t, p.Metrics(), "$ saved").Value <= 0 {
		t.Error("model lookup should be case-insensitive")
	}
}

// TestPrune_ByteExactAgainstJSONReconstruction is the real byte-exactness check.
// The earlier test asserted only that some fragments survived and the body got
// shorter, which passes even if the rewrite reflows the whole document — and it
// removed only a middle element, so the two comma cases that actually differ
// (first and last) were never exercised.
//
// Here the expected output is built by deleting the same elements from the
// ORIGINAL bytes by hand, so any reformatting, key reordering or whitespace
// change fails. Also validates the result with encoding/json, which nothing did.
func TestPrune_ByteExactAgainstJSONReconstruction(t *testing.T) {
	const orig = `{"model":"m","tools":[{"name":"A","x":1},{"name":"B","x":2},{"name":"C","x":3}],"max_tokens":8}`
	cases := []struct {
		remove []string
		want   string
	}{
		{[]string{"A"}, `{"model":"m","tools":[{"name":"B","x":2},{"name":"C","x":3}],"max_tokens":8}`},
		{[]string{"C"}, `{"model":"m","tools":[{"name":"A","x":1},{"name":"B","x":2}],"max_tokens":8}`},
		{[]string{"B"}, `{"model":"m","tools":[{"name":"A","x":1},{"name":"C","x":3}],"max_tokens":8}`},
		{[]string{"A", "C"}, `{"model":"m","tools":[{"name":"B","x":2}],"max_tokens":8}`},
	}
	for _, tc := range cases {
		t.Run(strings.Join(tc.remove, "+"), func(t *testing.T) {
			p := configured(t, tc.remove...)
			pctx := inferenceCtx("/v1/messages", orig, "A", "B", "C")
			run(t, p, pctx)
			if got := string(pctx.Body); got != tc.want {
				t.Errorf("byte mismatch\n got: %s\nwant: %s", got, tc.want)
			}
			var any map[string]any
			if err := json.Unmarshal(pctx.Body, &any); err != nil {
				t.Errorf("result is not valid JSON: %v", err)
			}
		})
	}
}

// TestPrune_ToolChoiceStringForms: "required" / "any" force no specific tool, so
// they must not suppress pruning; an object naming nothing recognisable must.
func TestPrune_ToolChoiceStringForms(t *testing.T) {
	body := `{"tools":[{"name":"Read"},{"name":"NotebookEdit"}],"tool_choice":%s}`
	for _, tc := range []struct {
		choice     string
		wantPruned bool
	}{
		{`"required"`, true},
		{`"any"`, true},
		{`{"type":"required"}`, true},
		{`{"tool":{"name":"NotebookEdit"}}`, false}, // Bedrock-style forced tool: kept
		{`{"unknown_shape":true}`, false},           // cannot interpret: decline
	} {
		t.Run(tc.choice, func(t *testing.T) {
			p := configured(t, "NotebookEdit")
			pctx := inferenceCtx("/v1/messages", fmt.Sprintf(body, tc.choice), "Read", "NotebookEdit")
			run(t, p, pctx)
			pruned := !strings.Contains(string(pctx.Body), "NotebookEdit")
			if pruned != tc.wantPruned {
				t.Errorf("tool_choice %s: pruned=%v want %v — body: %s", tc.choice, pruned, tc.wantPruned, pctx.Body)
			}
		})
	}
}

// TestPrune_OpenAIDialectAllRemoved: the all-removed path drops tools and
// tool_choice, and must do so for the OpenAI shape too.
func TestPrune_OpenAIDialectAllRemoved(t *testing.T) {
	body := `{"model":"m","tools":[{"type":"function","function":{"name":"A"}},` +
		`{"type":"function","function":{"name":"B"}}],"tool_choice":"auto"}`
	p := configured(t, "A", "B")
	pctx := inferenceCtx("/v1/chat/completions", body, "A", "B")
	run(t, p, pctx)
	got := string(pctx.Body)
	if strings.Contains(got, "tools") || strings.Contains(got, "tool_choice") {
		t.Errorf("both keys should be dropped: %s", got)
	}
	var any map[string]any
	if err := json.Unmarshal(pctx.Body, &any); err != nil {
		t.Errorf("result is not valid JSON: %v", err)
	}
}

// TestPricing_PartialModelConfigIsUnpriced: set() ORs the three rate fields, so a
// model configured with only a cache-read rate used to resolve as "priced" and
// then return 0 for a cache-write request — charging zero into the total while
// counting toward the priced denominator, so the saving vanished with no
// `requests unpriced` row to show it had.
func TestPricing_PartialModelConfigIsUnpriced(t *testing.T) {
	p := configuredJSON(t, `{"remove":["NotebookEdit"],
	  "pricing":{"some-model":{"cache_read_cost_per_token":1e-06}}}`)
	pruneWithModel(t, p, "some-model") // pruneWithModel finishes as a cache WRITE

	gap := findMetric(t, p.Metrics(), "requests unpriced")
	if gap.Value != 1 {
		t.Errorf("requests unpriced = %v, want 1 — no cache-write rate is configured", gap.Value)
	}
	for _, m := range p.Metrics() {
		if m.Name == "$ saved" {
			t.Errorf("$ saved = %v, want no row rather than a zero charged into the total", m.Value)
		}
	}
}

// TestPricing_BuiltInTableBeatsFlatFallback: the flat fields are documented as
// covering "models absent from pricing", and a model in the built-in table is not
// absent. Letting one flat input rate shadow every per-model default would
// reintroduce the flat-rate mispricing the table exists to avoid — and silently,
// since the figure would then claim to be operator-configured.
func TestPricing_BuiltInTableBeatsFlatFallback(t *testing.T) {
	p := configuredJSON(t, `{"remove":["NotebookEdit"],"input_cost_per_token":9e-05}`)
	rates, src := p.cfg.ratesFor("claude-opus-5")
	if src != rateDefault {
		t.Errorf("source = %v, want rateDefault for a model in the built-in table", src)
	}
	if rates.InputCostPerToken == 9e-05 {
		t.Error("flat fallback shadowed the built-in per-model rate")
	}
	// A model in neither table still uses the flat fallback.
	_, src2 := p.cfg.ratesFor("no-such-model")
	if src2 != rateConfigured {
		t.Errorf("source = %v, want rateConfigured via the flat fallback", src2)
	}
	// And the caveat is still attached, because defaults were used.
	pruneWithModel(t, p, "claude-opus-5")
	if m := findMetric(t, p.Metrics(), "$ saved"); !strings.Contains(m.Note, "built-in rates") {
		t.Errorf("note = %q, want the built-in-rates caveat", m.Note)
	}
}

// TestPrune_KeepsToolsCitedByHistory: a provider may reject a request whose
// history references a tool the manifest no longer defines. This arises exactly
// when the plugin is enabled mid-conversation — the config hot-reloads, and the
// scan's rolling window can propose a tool used earlier in the same session.
func TestPrune_KeepsToolsCitedByHistory(t *testing.T) {
	body := `{"tools":[{"name":"Read"},{"name":"WebSearch"},{"name":"NotebookEdit"}],
	  "messages":[
	    {"role":"user","content":"go"},
	    {"role":"assistant","content":[{"type":"tool_use","id":"t1","name":"WebSearch","input":{}}]},
	    {"role":"user","content":[{"type":"tool_result","tool_use_id":"t1","content":"ok"}]}]}`
	p := configured(t, "WebSearch", "NotebookEdit")
	pctx := inferenceCtx("/v1/messages", body, "Read", "WebSearch", "NotebookEdit")
	run(t, p, pctx)

	got := string(pctx.Body)
	if !strings.Contains(got, "WebSearch") {
		t.Errorf("WebSearch is cited by history and must survive:\n%s", got)
	}
	if strings.Contains(got, "NotebookEdit") {
		t.Errorf("NotebookEdit is uncited and should still be pruned:\n%s", got)
	}
}

// TestPrune_PreservesCacheControlBreakpoint: a prompt-cache breakpoint rides on
// one element — Claude Code marks the last tool. Deleting that element deletes
// the breakpoint, and losing it turns every later turn into a full cache write,
// which costs far more than the definitions saved. The marker must move to the
// last surviving tool.
func TestPrune_PreservesCacheControlBreakpoint(t *testing.T) {
	body := `{"tools":[{"name":"Read"},{"name":"Bash"},` +
		`{"name":"NotebookEdit","cache_control":{"type":"ephemeral"}}]}`
	p := configured(t, "NotebookEdit")
	pctx := inferenceCtx("/v1/messages", body, "Read", "Bash", "NotebookEdit")
	run(t, p, pctx)

	got := string(pctx.Body)
	if strings.Contains(got, "NotebookEdit") {
		t.Fatalf("the tool should be pruned: %s", got)
	}
	if !strings.Contains(got, "cache_control") {
		t.Errorf("cache breakpoint was destroyed — every later turn becomes a full cache write:\n%s", got)
	}
	// It must land on the LAST surviving tool, where the prefix ends.
	if !strings.Contains(got, `{"name":"Bash","cache_control":{"type":"ephemeral"}}`) {
		t.Errorf("marker not on the last survivor:\n%s", got)
	}
	var any map[string]any
	if err := json.Unmarshal(pctx.Body, &any); err != nil {
		t.Errorf("invalid JSON after the move: %v", err)
	}
}

// TestPrune_DoesNotDuplicateCacheControl: when a surviving tool already carries a
// breakpoint, adding another would exceed the provider's cache_control limit.
func TestPrune_DoesNotDuplicateCacheControl(t *testing.T) {
	body := `{"tools":[{"name":"Read","cache_control":{"type":"ephemeral"}},` +
		`{"name":"NotebookEdit","cache_control":{"type":"ephemeral"}}]}`
	p := configured(t, "NotebookEdit")
	pctx := inferenceCtx("/v1/messages", body, "Read", "NotebookEdit")
	run(t, p, pctx)
	if n := strings.Count(string(pctx.Body), "cache_control"); n != 1 {
		t.Errorf("cache_control appears %d times, want 1: %s", n, pctx.Body)
	}
}

// TestNoteDrift_EmptyFirstManifestDoesNotSpendTheOnce: sync.Once marks itself
// done however the closure returns, so an early return on an empty manifest used
// to disable the stale-list warning permanently — and an empty first manifest is
// the norm on dialects whose tool names the plugin cannot read (Gemini
// functionDeclarations, Bedrock toolSpec nesting). A stale remove list then stayed
// silent in exactly the deployments most likely to have one.
func TestNoteDrift_EmptyFirstManifestDoesNotSpendTheOnce(t *testing.T) {
	p := configured(t, "NeverOffered")

	// Nothing observed: must not consume the guard.
	p.noteDrift(nil)
	p.noteDrift([]pipeline.InferenceTool{})
	if p.driftChecked {
		t.Fatal("the check claims to have run on an empty manifest")
	}

	// A real manifest afterwards must still reach the check.
	p.noteDrift([]pipeline.InferenceTool{{Name: "Read"}})
	if !p.driftChecked {
		t.Error("the check never ran on the first non-empty manifest — an empty one had spent the Once")
	}
}

// TestMetrics_DollarRowsDiscloseTheyAreGross: changing the remove list changes the
// cached prefix, so the next request re-writes the whole prefix at ~1.25x input
// while the recurring saving is ~0.1x on a small delta — tens of requests to break
// even after each change. Counters also reset on the reload that applies the
// change, so the re-warm is invisible exactly when it is paid. A figure that does
// not say it is gross reads as net.
func TestMetrics_DollarRowsDiscloseTheyAreGross(t *testing.T) {
	p := configured(t, "NotebookEdit")
	pruneWithModel(t, p, "claude-opus-5")
	for _, name := range []string{"$ saved", "$ saved / request"} {
		m := findMetric(t, p.Metrics(), name)
		if !strings.Contains(m.Note, "gross") || !strings.Contains(m.Note, "re-warm") {
			t.Errorf("%s note = %q, want it to disclose the figure is gross of cache re-warm", name, m.Note)
		}
	}
}

// TestPricingPatternsCoverRealModels pins the pattern keys against the actual
// model list the rossoctl LiteLLM gateway serves, including provider prefixes
// and dated suffixes. The point of this test is the regression it prevents: a
// provider version bump must not silently drop a family to unpriced.
func TestPricingPatternsCoverRealModels(t *testing.T) {
	cases := []struct {
		model string
		want  float64 // input rate
	}{
		// opus family, across versions and prefixes
		{"claude-opus-5", 0.0000038},
		{"claude-opus-4-8", 0.0000038},
		{"claude-opus-4-7", 0.0000038},
		{"claude-opus-4-6", 0.0000038},
		{"aws/claude-opus-5", 0.0000038},
		{"aws/claude-opus-4-7", 0.0000038},
		// a version that does not exist yet must still price
		{"claude-opus-9", 0.0000038},
		{"claude-opus-5-20260901", 0.0000038},
		// sonnet
		{"claude-sonnet-5", 0.00000152},
		{"claude-sonnet-4-6", 0.00000152},
		{"aws/claude-sonnet-4-5", 0.00000152},
		{"claude-sonnet-4-5-20250929", 0.00000152},
		// haiku
		{"claude-haiku-4-5", 0.00000076},
		{"aws/claude-haiku-4-5", 0.00000076},
		{"claude-haiku-4-5-20251001", 0.00000076},
	}
	c := &config{}
	c.applyDefaults()
	for _, tc := range cases {
		rates, src := c.ratesFor(tc.model)
		if src != rateDefault {
			t.Errorf("%s: source = %v, want rateDefault", tc.model, src)
			continue
		}
		if rates.InputCostPerToken != tc.want {
			t.Errorf("%s: input rate = %g, want %g", tc.model, rates.InputCostPerToken, tc.want)
		}
	}
	// A non-Claude model has no built-in rate and must report so rather than
	// borrowing a Claude family's numbers.
	if _, src := c.ratesFor("gpt-4o"); src != rateNone {
		t.Errorf("gpt-4o: source = %v, want rateNone", src)
	}
}

// TestPricingPatternPrecedence covers the resolution order that lets an operator
// pin one version without giving up family coverage.
func TestPricingPatternPrecedence(t *testing.T) {
	c := &config{Pricing: map[string]modelRates{
		// exact key: must win over both globs below
		"claude-opus-5": {InputCostPerToken: 1},
		// broad glob
		"*claude-opus-*": {InputCostPerToken: 2},
		// narrower glob: longer pattern wins among globs
		"*claude-opus-4-8*": {InputCostPerToken: 3},
	}}
	c.applyDefaults()
	if c.pricingErr != nil {
		t.Fatalf("compile: %v", c.pricingErr)
	}
	for _, tc := range []struct {
		model string
		want  float64
		src   rateSource
	}{
		{"claude-opus-5", 1, rateConfigured},   // exact beats glob
		{"claude-opus-4-8", 3, rateConfigured}, // longest glob wins
		{"claude-opus-4-6", 2, rateConfigured}, // broad glob
		// built-in pattern still covers a family the operator said nothing about
		{"claude-haiku-4-5", 0.00000076, rateDefault},
	} {
		rates, src := c.ratesFor(tc.model)
		if src != tc.src || rates.InputCostPerToken != tc.want {
			t.Errorf("%s: got (%g, %v), want (%g, %v)",
				tc.model, rates.InputCostPerToken, src, tc.want, tc.src)
		}
	}
}

// TestPricingPatternMatchesCase guards the lowercasing on both sides: config
// keys and the model name off the wire.
func TestPricingPatternMatchesCase(t *testing.T) {
	c := &config{Pricing: map[string]modelRates{
		"*CLAUDE-OPUS-*": {InputCostPerToken: 7},
	}}
	c.applyDefaults()
	rates, src := c.ratesFor("AWS/Claude-Opus-5")
	if src != rateConfigured || rates.InputCostPerToken != 7 {
		t.Errorf("got (%g, %v), want (7, configured)", rates.InputCostPerToken, src)
	}
}

// TestPricingBadPatternRejected: a malformed glob must fail Configure loudly,
// not degrade to unpriced with no explanation.
func TestPricingBadPatternRejected(t *testing.T) {
	c := &config{Pricing: map[string]modelRates{
		"*claude-[opus": {InputCostPerToken: 1},
	}}
	c.applyDefaults()
	if c.pricingErr == nil {
		t.Fatal("want a compile error for an unterminated character class")
	}
}

// TestPricingPerMillionUnits is the natural-units path: an operator copies
// "$3.80 / Mtok" off a price list and the plugin prices with it, no hand
// division. Values are compared against the per-token equivalent to prove the
// conversion, not merely that something non-zero landed.
func TestPricingPerMillionUnits(t *testing.T) {
	c := &config{Pricing: map[string]modelRates{
		"my-model": {
			InputCostPerMillion:      3.80,
			CacheWriteCostPerMillion: 4.75,
			CacheReadCostPerMillion:  0.38,
		},
	}}
	c.applyDefaults()
	if c.pricingErr != nil {
		t.Fatalf("unexpected error: %v", c.pricingErr)
	}
	rates, src := c.ratesFor("my-model")
	if src != rateConfigured {
		t.Fatalf("source = %v, want rateConfigured", src)
	}
	// Compared with a tolerance, not for equality: a config value divides at
	// runtime, so 3.80/1e6 lands one ulp below the 3.8e-06 literal. That is a
	// 1e-16 relative difference on a dollar figure — not a property worth
	// pinning, and pinning it would only invite a fragile test.
	for _, tc := range []struct {
		tier tier
		want float64
	}{
		{tierInput, 0.0000038},
		{tierCacheWrite, 0.00000475},
		{tierCacheRead, 0.00000038},
	} {
		got, ok := rates.rateFor(tc.tier)
		if !ok || math.Abs(got-tc.want) > tc.want*1e-12 {
			t.Errorf("tier %v: got (%g, %v), want (~%g, true)", tc.tier, got, ok, tc.want)
		}
	}
}

// TestPricingPerMillionGlobAndFlat covers the two other places a rate can be
// stated, so per-million isn't quietly honoured in only one of the three.
func TestPricingPerMillionGlobAndFlat(t *testing.T) {
	c := &config{
		Pricing: map[string]modelRates{
			"*my-family-*": {InputCostPerMillion: 2.0},
		},
		InputCostPerMillion: 9.0,
	}
	c.applyDefaults()
	if c.pricingErr != nil {
		t.Fatalf("unexpected error: %v", c.pricingErr)
	}
	if r, src := c.ratesFor("my-family-7"); src != rateConfigured || r.InputCostPerToken != 2.0/1e6 {
		t.Errorf("glob: got (%g, %v), want (%g, configured)", r.InputCostPerToken, src, 2.0/1e6)
	}
	// A model no pattern claims falls to the flat rate, also stated per-million.
	if r, src := c.ratesFor("totally-unknown"); src != rateConfigured || r.InputCostPerToken != 9.0/1e6 {
		t.Errorf("flat: got (%g, %v), want (%g, configured)", r.InputCostPerToken, src, 9.0/1e6)
	}
}

// TestPricingUnitConflictRejected is the important one. The two units differ by
// 10^6, so silently preferring either would misprice by a millionfold with
// nothing in the readout to show which was honoured.
func TestPricingUnitConflictRejected(t *testing.T) {
	for _, tc := range []struct {
		name string
		r    modelRates
		want string
	}{
		{"input", modelRates{InputCostPerMillion: 3.8, InputCostPerToken: 0.0000038}, "input"},
		{"cache_write", modelRates{CacheWriteCostPerMillion: 4.75, CacheWriteCostPerToken: 0.00000475}, "cache_write"},
		{"cache_read", modelRates{CacheReadCostPerMillion: 0.38, CacheReadCostPerToken: 0.00000038}, "cache_read"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			c := &config{Pricing: map[string]modelRates{"m": tc.r}}
			c.applyDefaults()
			if c.pricingErr == nil {
				t.Fatal("want an error when both units are set for one tier")
			}
			// The message must name the tier, or an operator with three tiers
			// configured has to bisect to find the one at fault.
			if !strings.Contains(c.pricingErr.Error(), tc.want) {
				t.Errorf("error %q does not name tier %q", c.pricingErr, tc.want)
			}
		})
	}
	// Same rule on the flat fallback.
	c := &config{InputCostPerMillion: 3.8, InputCostPerToken: 0.0000038}
	c.applyDefaults()
	if c.pricingErr == nil {
		t.Fatal("flat fallback: want an error when both units are set")
	}
}

// TestPricingMixedUnitsAcrossTiersAllowed: stating different tiers in different
// units is odd but unambiguous, so it must not be rejected — the rule is about
// one tier stated twice, not about tidiness.
func TestPricingMixedUnitsAcrossTiersAllowed(t *testing.T) {
	c := &config{Pricing: map[string]modelRates{
		"m": {InputCostPerMillion: 3.80, CacheReadCostPerToken: 0.00000038},
	}}
	c.applyDefaults()
	if c.pricingErr != nil {
		t.Fatalf("unexpected error: %v", c.pricingErr)
	}
	r, _ := c.ratesFor("m")
	// per-million tier: runtime division, so tolerance (see TestPricingPerMillionUnits).
	if got, _ := r.rateFor(tierInput); math.Abs(got-0.0000038) > 1e-18 {
		t.Errorf("input = %g, want ~0.0000038", got)
	}
	// per-token tier: stored verbatim, so exact.
	if got, _ := r.rateFor(tierCacheRead); got != 0.00000038 {
		t.Errorf("cache read = %g, want 0.00000038", got)
	}
}

// TestPricingConfigureRejectsUnitConflict proves the fault reaches Configure
// rather than stopping at applyDefaults, since that is what actually fails boot.
func TestPricingConfigureRejectsUnitConflict(t *testing.T) {
	p := New()
	err := p.Configure([]byte(`{"pricing":{"m":{"input_cost_per_million":3.8,"input_cost_per_token":0.0000038}}}`))
	if err == nil {
		t.Fatal("Configure accepted a both-units entry")
	}
	if !strings.Contains(err.Error(), "input") {
		t.Errorf("error %q does not name the tier", err)
	}
}

// TestPricingPerMillionJSONDecodes guards the wire names an operator types.
func TestPricingPerMillionJSONDecodes(t *testing.T) {
	p := New()
	if err := p.Configure([]byte(`{
		"remove": ["X"],
		"pricing": {"*claude-opus-*": {
			"input_cost_per_million": 3.80,
			"cache_write_cost_per_million": 4.75,
			"cache_read_cost_per_million": 0.38
		}},
		"input_cost_per_million": 1.0
	}`)); err != nil {
		t.Fatalf("Configure: %v", err)
	}
	r, src := p.cfg.ratesFor("claude-opus-5")
	if src != rateConfigured || math.Abs(r.InputCostPerToken-0.0000038) > 1e-18 {
		t.Errorf("got (%g, %v), want (~0.0000038, configured)", r.InputCostPerToken, src)
	}
}

// TestBuiltinRatesMatchDocumentedPerMillion pins the built-in table against the
// exact per-Mtok figures the docs publish.
//
// Each expectation is written as a CONSTANT expression ($3.80 / tokensPerMillion),
// which the compiler folds exactly — the same way pricing.go does. So this fails
// both if a documented figure and the table drift apart, and if anyone converts
// the table with a runtime division instead, which lands a ulp low.
//
// It deliberately does not assert rate*1e6 == 3.80: multiplying back is a second
// rounding that isn't exact for every value (0.076 round-trips to
// 0.07600000000000001), which would make the test fail for a reason that has
// nothing to do with the table being right.
func TestBuiltinRatesMatchDocumentedPerMillion(t *testing.T) {
	c := &config{}
	c.applyDefaults()
	for _, tc := range []struct {
		model                     string
		input, cacheWr, cacheRead float64
	}{
		{"claude-opus-5", 3.80 / tokensPerMillion, 4.75 / tokensPerMillion, 0.38 / tokensPerMillion},
		{"claude-sonnet-5", 1.52 / tokensPerMillion, 1.90 / tokensPerMillion, 0.152 / tokensPerMillion},
		{"claude-haiku-4-5", 0.76 / tokensPerMillion, 0.95 / tokensPerMillion, 0.076 / tokensPerMillion},
	} {
		r, src := c.ratesFor(tc.model)
		if src != rateDefault {
			t.Errorf("%s: src = %v, want rateDefault", tc.model, src)
			continue
		}
		for _, f := range []struct {
			name      string
			got, want float64
		}{
			{"input", r.InputCostPerToken, tc.input},
			{"cache write", r.CacheWriteCostPerToken, tc.cacheWr},
			{"cache read", r.CacheReadCostPerToken, tc.cacheRead},
		} {
			if f.got != f.want {
				t.Errorf("%s %s: %v, want exactly %v ($%v/Mtok)",
					tc.model, f.name, f.got, f.want, f.want*tokensPerMillion)
			}
		}
	}
}
