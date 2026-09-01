// Package litellm_budgettrack provides a pipeline plugin that tracks
// per-request cost and enforces a daily spending budget, rejecting requests
// with HTTP 429 when the budget is exceeded.
//
// Cost is resolved in two ways:
//
//   - Non-streaming responses carry the cost in a response header
//     (x-litellm-response-cost, or the pre-discount -original variant), read
//     on the terminal frame.
//   - Streaming responses (text/event-stream — what Claude Code's
//     /v1/messages uses) report cost 0 in the header because the total is not
//     known when the headers are sent. For these, the plugin parses the token
//     usage out of the terminal SSE events (Anthropic message_delta /
//     message_stop, or OpenAI's final chunk usage) and prices it from the
//     configured per-token rates. Streaming cost tracking is therefore active
//     only when input_cost_per_token / output_cost_per_token are configured.
package litellm_budgettrack

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"math"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/rossoctl/cortex/authbridge/authlib/pipeline"
	"github.com/rossoctl/cortex/authbridge/authlib/plugins"
)

// Response cost headers emitted by LiteLLM.
//
// responseCostHeader is the effective (post-discount) cost and is present on
// OpenAI-style /v1/chat/completions responses. Newer LiteLLM releases — and the
// Anthropic /v1/messages endpoint that Claude Code uses — do not emit it, only
// the pre-discount "-original" variant, so we fall back to that when the bare
// header is absent. Without the fallback, budget tracking silently records $0
// for Anthropic-format traffic.
const (
	responseCostHeader         = "X-Litellm-Response-Cost"
	responseCostOriginalHeader = "X-Litellm-Response-Cost-Original"
)

type budgetTrackConfig struct {
	SpendFile string  `json:"spend_file" required:"true" description:"Path to the JSON spend ledger file."`
	MaxBudget float64 `json:"max_budget" required:"true" description:"Daily budget in USD."`
	// InputCostPerToken / OutputCostPerToken price streamed responses whose
	// header cost is 0 (the total is unknown when streaming headers are sent).
	// USD per token; optional. When both are zero, streamed responses cannot be
	// priced and contribute 0 to the ledger.
	InputCostPerToken  float64 `json:"input_cost_per_token" description:"USD per uncached input token, for pricing streamed responses."`
	OutputCostPerToken float64 `json:"output_cost_per_token" description:"USD per output/completion token, for pricing streamed responses."`
	// Prompt-cache tiers are priced separately: providers charge a premium to
	// WRITE a cache entry and a steep discount to READ one (see
	// pipeline/extensions.go). When unset (0) each defaults to
	// InputCostPerToken, reproducing a flat rate — which overstates cache-heavy
	// traffic (e.g. Claude Code) by up to ~10×. Set them for accurate pricing.
	CacheWriteCostPerToken float64 `json:"cache_write_cost_per_token" description:"USD per cache-write (creation) input token; defaults to input_cost_per_token."`
	CacheReadCostPerToken  float64 `json:"cache_read_cost_per_token" description:"USD per cache-read input token; defaults to input_cost_per_token."`
}

// cacheWriteRate / cacheReadRate return the effective per-token rate for each
// cache tier, defaulting to the uncached input rate when unset (0).
func (c budgetTrackConfig) cacheWriteRate() float64 {
	if c.CacheWriteCostPerToken > 0 {
		return c.CacheWriteCostPerToken
	}
	return c.InputCostPerToken
}

func (c budgetTrackConfig) cacheReadRate() float64 {
	if c.CacheReadCostPerToken > 0 {
		return c.CacheReadCostPerToken
	}
	return c.InputCostPerToken
}

// stateKey names the per-request scratch holding token usage accumulated across
// streaming frames until the terminal frame prices it.
const stateKey = "litellm-budget-track"

// usageState accumulates the largest token counts seen across a stream's
// frames. Anthropic reports input_tokens in message_start and the cumulative
// output_tokens in the final message_delta, so taking the max of each yields
// the final totals; OpenAI reports both together in its terminal usage chunk.
type usageState struct {
	uncachedInputTokens int
	cacheWriteTokens    int // cache_creation_input_tokens
	cacheReadTokens     int // cache_read_input_tokens
	outputTokens        int
	settled             bool // terminal frame already priced this request (exactly-once)
}

type spendLedger struct {
	Date       string  `json:"date"`
	TotalSpend float64 `json:"total_spend"`
	TotalCalls int     `json:"total_calls"`
}

// BudgetTrack enforces a daily spending budget based on x-litellm-response-cost.
type BudgetTrack struct {
	cfg    budgetTrackConfig
	mu     sync.Mutex
	ledger spendLedger
}

// New creates an unconfigured BudgetTrack plugin instance.
func New() *BudgetTrack { return &BudgetTrack{} }

func init() {
	plugins.RegisterPlugin("litellm-budget-track", func() pipeline.Plugin { return New() })
}

func (p *BudgetTrack) Name() string { return "litellm-budget-track" }

func (p *BudgetTrack) Capabilities() pipeline.PluginCapabilities {
	return pipeline.PluginCapabilities{
		// ReadsBody: the plugin parses the response body (streamed usage). It
		// makes Pipeline.NeedsBody() true so the extproc (envoy-sidecar) listener
		// buffers the response body and takes its body-phase branch; without it
		// that listener dispatches a single header-only RunResponseFrame and the
		// streamed accounting silently records nothing (or double-charges if
		// Envoy is statically configured BUFFERED). The proxy listeners gate on
		// HasStreamingResponders() and are unaffected. Mirrors inference-parser.
		ReadsBody:   true,
		Description: "Track LLM cost (response header or streamed usage) and enforce a daily budget.",
	}
}

func (p *BudgetTrack) Configure(raw json.RawMessage) error {
	if err := json.Unmarshal(raw, &p.cfg); err != nil {
		return fmt.Errorf("litellm-budget-track config: %w", err)
	}
	if p.cfg.SpendFile == "" {
		return fmt.Errorf("litellm-budget-track: spend_file is required")
	}
	if p.cfg.MaxBudget <= 0 {
		return fmt.Errorf("litellm-budget-track: max_budget must be > 0")
	}
	// Per-token rates must be finite and non-negative. A negative rate would make
	// a streamed request's cost negative, which accumulate() drops — so the request
	// would silently neither charge budget nor record a call. Reject at config time.
	for name, rate := range map[string]float64{
		"input_cost_per_token":       p.cfg.InputCostPerToken,
		"output_cost_per_token":      p.cfg.OutputCostPerToken,
		"cache_write_cost_per_token": p.cfg.CacheWriteCostPerToken,
		"cache_read_cost_per_token":  p.cfg.CacheReadCostPerToken,
	} {
		if rate < 0 || math.IsNaN(rate) || math.IsInf(rate, 0) {
			return fmt.Errorf("litellm-budget-track: %s must be finite and >= 0", name)
		}
	}
	p.loadLedger()
	return nil
}

// OnRequest checks if the daily budget has been exceeded before allowing the request.
func (p *BudgetTrack) OnRequest(_ context.Context, pctx *pipeline.Context) pipeline.Action {
	p.mu.Lock()
	p.resetIfNewDay()
	spend := p.ledger.TotalSpend
	p.mu.Unlock()

	if spend >= p.cfg.MaxBudget {
		return pipeline.DenyStatus(429, "budget.exceeded",
			fmt.Sprintf("Cortex ExceededTokenBudget: daily spend $%.4f exceeds budget $%.2f. Reset at midnight UTC.", spend, p.cfg.MaxBudget))
	}
	return pipeline.Action{Type: pipeline.Continue}
}

// OnResponse handles the buffered path on listeners that do not route through
// OnResponseFrame. On the proxy listeners this plugin is a StreamingResponder,
// so pipeline.RunResponse skips it and OnResponseFrame drives accumulation
// instead; this remains for listeners that only call OnResponse.
func (p *BudgetTrack) OnResponse(_ context.Context, pctx *pipeline.Context) pipeline.Action {
	if cost, _ := headerCost(pctx); cost > 0 {
		p.accumulate(cost)
	}
	return pipeline.Action{Type: pipeline.Continue}
}

// OnResponseFrame observes each response frame. It parses token usage out of
// streamed SSE frames and, on the terminal frame, prices the request: the
// response-header cost when present (non-streaming), otherwise the parsed
// usage times the configured per-token rates (streaming).
func (p *BudgetTrack) OnResponseFrame(_ context.Context, pctx *pipeline.Context, frame []byte, last bool) pipeline.Action {
	if u, ok := parseFrameUsage(frame); ok {
		st := pipeline.GetState[usageState](pctx, stateKey)
		if st == nil {
			st = &usageState{}
			pipeline.SetState(pctx, stateKey, st)
		}
		// Max per bucket across frames: Anthropic reports uncached input in
		// message_start and the finalized cache counts + output in message_delta.
		if u.uncached > st.uncachedInputTokens {
			st.uncachedInputTokens = u.uncached
		}
		if u.cacheWrite > st.cacheWriteTokens {
			st.cacheWriteTokens = u.cacheWrite
		}
		if u.cacheRead > st.cacheReadTokens {
			st.cacheReadTokens = u.cacheRead
		}
		if u.output > st.outputTokens {
			st.outputTokens = u.output
		}
	}
	if !last {
		return pipeline.Action{Type: pipeline.Continue}
	}

	// Terminal frame: settle the cost exactly once. Materialize the scratch
	// unconditionally (a header-only response never allocated it above) so the
	// guard also covers that path — a listener that dispatches last=true twice
	// (e.g. extproc header + buffered-body phases) must not double-charge.
	st := pipeline.GetState[usageState](pctx, stateKey)
	if st == nil {
		st = &usageState{}
		pipeline.SetState(pctx, stateKey, st)
	}
	if st.settled {
		return pipeline.Action{Type: pipeline.Continue}
	}
	st.settled = true

	cost, present := headerCost(pctx)
	if cost <= 0 {
		// Fall back to per-token pricing only when there is no authoritative
		// header cost: the header is absent, or this is a streamed response
		// (where LiteLLM always reports 0). A present "0" on a non-streamed
		// response is a genuine free call (cache hit / error) — charge nothing,
		// don't invent a cost from the usage block.
		if !present || isEventStream(pctx) {
			// Price each prompt-cache tier at its own rate; cache rates default
			// to the uncached input rate when unset. Flat pricing would overstate
			// cache-heavy traffic (Claude Code) by up to ~10×.
			cost = float64(st.uncachedInputTokens)*p.cfg.InputCostPerToken +
				float64(st.cacheWriteTokens)*p.cfg.cacheWriteRate() +
				float64(st.cacheReadTokens)*p.cfg.cacheReadRate() +
				float64(st.outputTokens)*p.cfg.OutputCostPerToken
		}
	}
	if cost > 0 {
		p.accumulate(cost)
	}
	return pipeline.Action{Type: pipeline.Continue}
}

// accumulate adds one priced call to today's ledger and persists it. A
// non-finite or non-positive cost is ignored: NaN/±Inf would poison
// TotalSpend (making the budget check meaningless) and break the JSON
// marshal, so this is the single chokepoint that guarantees the ledger
// only ever holds finite money.
func (p *BudgetTrack) accumulate(cost float64) {
	if cost <= 0 || math.IsNaN(cost) || math.IsInf(cost, 0) {
		return
	}
	p.mu.Lock()
	p.resetIfNewDay()
	p.ledger.TotalSpend += cost
	p.ledger.TotalCalls++
	p.saveLedger()
	p.mu.Unlock()
}

// headerCost returns the usable positive cost reported in the response headers
// and whether a cost header was present at all. present distinguishes "no
// header" (fall back to usage pricing) from "header says 0" (a genuine free
// call — cache hit / error — that must NOT be re-priced from usage). A present
// but non-positive/non-finite header yields (0, true).
func headerCost(pctx *pipeline.Context) (cost float64, present bool) {
	costStr := pctx.ResponseHeaders.Get(responseCostHeader)
	if costStr == "" {
		// Anthropic /v1/messages (and newer LiteLLM) omit the bare header.
		costStr = pctx.ResponseHeaders.Get(responseCostOriginalHeader)
	}
	if costStr == "" {
		return 0, false
	}
	c, err := strconv.ParseFloat(costStr, 64)
	// strconv.ParseFloat accepts "NaN" / "Inf"; reject non-finite (and
	// non-positive) so a garbage or zero header does not poison the ledger. The
	// header was still present, so report that.
	if err != nil || c <= 0 || math.IsNaN(c) || math.IsInf(c, 0) {
		return 0, true
	}
	return c, true
}

// isEventStream reports whether the response is a text/event-stream (SSE) — the
// streamed shape where LiteLLM reports cost 0 in the header, so usage-based
// pricing is the intended fallback.
func isEventStream(pctx *pipeline.Context) bool {
	ct := pctx.ResponseHeaders.Get("Content-Type")
	if i := strings.IndexByte(ct, ';'); i >= 0 {
		ct = ct[:i]
	}
	return strings.EqualFold(strings.TrimSpace(ct), "text/event-stream")
}

// frameUsage is the per-prompt-cache-tier token breakdown extracted from a
// response frame. Uncached input, cache writes, and cache reads are kept
// separate because providers price them very differently.
type frameUsage struct {
	uncached   int // uncached input / OpenAI prompt tokens
	cacheWrite int // cache_creation_input_tokens
	cacheRead  int // cache_read_input_tokens
	output     int // output / completion tokens
}

// parseFrameUsage extracts the token usage breakdown from a response frame,
// covering Anthropic (usage, or message.usage in message_start) and OpenAI
// (usage.prompt_tokens / completion_tokens). Returns the largest count seen per
// bucket, and whether any usage was found.
//
// The listener's sseframe reader strips the "data:" prefix and returns the
// bare payload, so a streamed frame arrives as raw JSON. The buffered
// application/json path also delivers the whole body as one raw-JSON frame.
// We therefore try the frame as JSON directly, and also scan any "data:"
// lines for the case a frame still carries SSE framing.
func parseFrameUsage(frame []byte) (fu frameUsage, found bool) {
	consider := func(b []byte) {
		b = bytes.TrimSpace(b)
		if len(b) == 0 || b[0] != '{' {
			return
		}
		var ev struct {
			Usage   *usageJSON `json:"usage"`
			Message *struct {
				Usage *usageJSON `json:"usage"`
			} `json:"message"`
		}
		if json.Unmarshal(b, &ev) != nil {
			return
		}
		u := ev.Usage
		if u == nil && ev.Message != nil {
			u = ev.Message.Usage // Anthropic message_start nests usage
		}
		if u == nil {
			return
		}
		if v := u.uncachedInput(); v > fu.uncached {
			fu.uncached, found = v, true
		}
		if v := u.CacheCreationInputTokens; v > fu.cacheWrite {
			fu.cacheWrite, found = v, true
		}
		if v := u.CacheReadInputTokens; v > fu.cacheRead {
			fu.cacheRead, found = v, true
		}
		if v := u.outputTotal(); v > fu.output {
			fu.output, found = v, true
		}
	}

	consider(frame) // bare-JSON frame (sseframe payload, or buffered body)
	for _, line := range bytes.Split(frame, []byte("\n")) {
		if line = bytes.TrimSpace(line); bytes.HasPrefix(line, []byte("data:")) {
			consider(bytes.TrimPrefix(line, []byte("data:")))
		}
	}
	return fu, found
}

// usageJSON accepts both Anthropic and OpenAI usage shapes.
type usageJSON struct {
	InputTokens              int `json:"input_tokens"`
	OutputTokens             int `json:"output_tokens"`
	CacheCreationInputTokens int `json:"cache_creation_input_tokens"`
	CacheReadInputTokens     int `json:"cache_read_input_tokens"`
	PromptTokens             int `json:"prompt_tokens"`
	CompletionTokens         int `json:"completion_tokens"`
}

// uncachedInput is the input NOT served from / written to cache. Anthropic's
// input_tokens excludes the cache_* counts; OpenAI's prompt_tokens carries no
// cache split, so it counts as uncached.
func (u usageJSON) uncachedInput() int { return u.InputTokens + u.PromptTokens }

func (u usageJSON) outputTotal() int { return u.OutputTokens + u.CompletionTokens }

func (p *BudgetTrack) todayUTC() string {
	return time.Now().UTC().Format("2006-01-02")
}

func (p *BudgetTrack) resetIfNewDay() {
	today := p.todayUTC()
	if p.ledger.Date != today {
		p.ledger = spendLedger{Date: today}
	}
}

func (p *BudgetTrack) loadLedger() {
	data, err := os.ReadFile(p.cfg.SpendFile)
	if err != nil {
		p.ledger = spendLedger{Date: p.todayUTC()}
		return
	}
	var l spendLedger
	if json.Unmarshal(data, &l) != nil || l.Date != p.todayUTC() {
		p.ledger = spendLedger{Date: p.todayUTC()}
		return
	}
	p.ledger = l
}

func (p *BudgetTrack) saveLedger() {
	data, err := json.MarshalIndent(p.ledger, "", "  ")
	if err != nil {
		// Never overwrite a good ledger with a failed marshal (e.g. a
		// non-finite TotalSpend that slipped through). accumulate already
		// rejects non-finite costs; this is the belt-and-suspenders guard.
		return
	}
	_ = os.WriteFile(p.cfg.SpendFile, data, 0644)
}

var (
	_ pipeline.Plugin             = (*BudgetTrack)(nil)
	_ pipeline.Configurable       = (*BudgetTrack)(nil)
	_ pipeline.StreamingResponder = (*BudgetTrack)(nil)
)
