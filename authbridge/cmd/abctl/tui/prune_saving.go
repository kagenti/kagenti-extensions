package tui

import (
	"encoding/json"
	"fmt"

	"github.com/rossoctl/cortex/authbridge/authlib/pipeline"
)

// pruneSaving is the tool-prune per-request event as published on the request
// event under "tool-prune". Mirrors the plugin's shape; a decode test guards the
// tags.
type pruneSaving struct {
	BytesRemoved   int     `json:"bytesRemoved"`
	BodyBytesAfter int     `json:"bodyBytesAfter"`
	RateInput      float64 `json:"rateInput"`
	RateCacheWrite float64 `json:"rateCacheWrite"`
	RateCacheRead  float64 `json:"rateCacheRead"`
	RateSource     string  `json:"rateSource"`
	// Projected marks observe mode: the saving was measured but the bytes were
	// not actually removed, so it must not render as money already not spent.
	Projected bool `json:"projected"`
}

// decodePruneSaving pulls the tool-prune event off a request event, if present.
func decodePruneSaving(e *pipeline.SessionEvent) (pruneSaving, bool) {
	if e == nil || len(e.Plugins) == 0 {
		return pruneSaving{}, false
	}
	raw, ok := e.Plugins["tool-prune"]
	if !ok {
		return pruneSaving{}, false
	}
	var ps pruneSaving
	if err := json.Unmarshal(raw, &ps); err != nil || ps.BytesRemoved <= 0 || ps.BodyBytesAfter <= 0 {
		return pruneSaving{}, false
	}
	return ps, true
}

// savedTokensAndCost converts a request's byte saving into tokens and dollars,
// using the response's own usage.
//
// The two halves live on different events by necessity: the byte saving is known
// when the request is rewritten, and the tier it came out of — and the ratio to
// convert bytes to tokens — only from the response. So this is the last step of
// an arithmetic the plugin starts.
//
// Tier matters more than it looks: providers charge ~1.25x the input rate for a
// cache write and ~0.1x for a cache read, so the same saved bytes are worth over
// 12x more on a cache miss than a hit. Picking the tier the request actually
// used is the difference between a figure and a guess.
func savedTokensAndCost(ps pruneSaving, resp *pipeline.InferenceExtension) (tokens, usd float64, ok bool) {
	if resp == nil {
		return 0, 0, false
	}
	prompt := resp.InputTokens + resp.CacheReadTokens + resp.CacheWriteTokens
	if prompt == 0 {
		prompt = resp.PromptTokens // provider reported only an aggregate
	}
	if prompt <= 0 {
		return 0, 0, false
	}
	// The plugin calibrates on the request it just sent: prompt tokens over the
	// post-prune body size, both measured on the same request so the two sides
	// agree.
	tokens = float64(ps.BytesRemoved) * float64(prompt) / float64(ps.BodyBytesAfter)

	rate := ps.RateInput
	switch {
	case resp.CacheWriteTokens > resp.CacheReadTokens && resp.CacheWriteTokens > 0:
		rate = ps.RateCacheWrite
	case resp.CacheReadTokens > 0:
		rate = ps.RateCacheRead
	}
	return tokens, tokens * rate, true
}

// formatSavedOnly renders a request row's saving: what was removed and what it
// was worth. No total, because a request has no billed token count — that
// belongs to the response, on its own row.
//
// A projected saving (on_error: observe, where the bytes were measured but not
// removed) is prefixed "~" and drops the "−". Rendering it identically to a real
// saving would invite an operator to add up money that was still spent, and
// observe mode exists precisely to be trusted while it is not yet enforcing.
func formatSavedOnly(tokens, usd float64, rateSource string, projected bool) string {
	if tokens <= 0 {
		return ""
	}
	cell := "−" + formatCompact(tokens)
	if projected {
		cell = "~" + formatCompact(tokens)
	}
	if usd > 0 && rateSource != "none" {
		cell += fmt.Sprintf("  $%s", formatUSD(usd))
	}
	return cell
}

// formatCompact renders a token count tersely enough for a table cell: 10577
// becomes "10.6k". Exact below 1000, where the extra digits still fit.
func formatCompact(v float64) string {
	switch {
	case v >= 1_000_000:
		return fmt.Sprintf("%.1fM", v/1_000_000)
	case v >= 1_000:
		return fmt.Sprintf("%.1fk", v/1_000)
	default:
		return fmt.Sprintf("%.0f", v)
	}
}

// formatUSD keeps small amounts legible: a per-request saving is often fractions
// of a cent, where %.2f would round every row to "0.00".
func formatUSD(v float64) string {
	switch {
	case v >= 1:
		return fmt.Sprintf("%.2f", v)
	case v >= 0.01:
		return fmt.Sprintf("%.3f", v)
	default:
		return fmt.Sprintf("%.4f", v)
	}
}
