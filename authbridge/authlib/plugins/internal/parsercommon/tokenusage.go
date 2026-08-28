package parsercommon

import "github.com/rossoctl/cortex/authbridge/authlib/pipeline"

// TokenUsage is the provider-neutral token accounting shape. Provider
// parsers normalize their wire format (e.g. OpenAI's inclusive
// prompt_tokens) into these fields before publishing via Fill.
type TokenUsage struct {
	Input      int // uncached prompt tokens
	CacheRead  int // prompt tokens served from cache
	CacheWrite int // prompt tokens written to cache
	Output     int // generated completion tokens
	Reasoning  int // reasoning-only output (subset of Output)
}

// PromptTotal is the sum of all prompt-side sub-kinds.
func (u TokenUsage) PromptTotal() int {
	return u.Input + u.CacheRead + u.CacheWrite
}

// Total is PromptTotal + Output. Reasoning is a subset of Output and
// intentionally not added.
func (u TokenUsage) Total() int {
	return u.PromptTotal() + u.Output
}

// Fill writes the split counters and derived legacy aggregates onto ext.
func (u TokenUsage) Fill(ext *pipeline.InferenceExtension) {
	ext.InputTokens = u.Input
	ext.CacheReadTokens = u.CacheRead
	ext.CacheWriteTokens = u.CacheWrite
	ext.OutputTokens = u.Output
	ext.ReasoningTokens = u.Reasoning

	ext.PromptTokens = u.PromptTotal()
	ext.CompletionTokens = u.Output
	ext.TotalTokens = u.Total()
}
