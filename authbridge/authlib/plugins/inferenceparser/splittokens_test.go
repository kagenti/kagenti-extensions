package inferenceparser

import (
	"fmt"
	"testing"

	"github.com/rossoctl/cortex/authbridge/authlib/pipeline"
)

// checkSplit surfaces every split field in one error message so a
// mismatched counter is obvious.
func checkSplit(t *testing.T, ext *pipeline.InferenceExtension, want pipeline.InferenceExtension) {
	t.Helper()
	got := fmt.Sprintf("input=%d cache_read=%d cache_write=%d output=%d reasoning=%d prompt=%d completion=%d total=%d",
		ext.InputTokens, ext.CacheReadTokens, ext.CacheWriteTokens, ext.OutputTokens, ext.ReasoningTokens,
		ext.PromptTokens, ext.CompletionTokens, ext.TotalTokens)
	expected := fmt.Sprintf("input=%d cache_read=%d cache_write=%d output=%d reasoning=%d prompt=%d completion=%d total=%d",
		want.InputTokens, want.CacheReadTokens, want.CacheWriteTokens, want.OutputTokens, want.ReasoningTokens,
		want.PromptTokens, want.CompletionTokens, want.TotalTokens)
	if got != expected {
		t.Errorf("split token counters mismatch\n got: %s\nwant: %s", got, expected)
	}
}

// Non-streaming Anthropic response with both cache_read and cache_write.
func TestSplitTokens_AnthropicJSON(t *testing.T) {
	ext := &pipeline.InferenceExtension{Model: "claude-opus-4-8"}
	body := []byte(`{
		"content": [{"type": "text", "text": "ok"}],
		"stop_reason": "end_turn",
		"usage": {
			"input_tokens": 50,
			"cache_creation_input_tokens": 100,
			"cache_read_input_tokens": 200,
			"output_tokens": 25
		}
	}`)
	parseAnthropicJSON(body, ext)

	checkSplit(t, ext, pipeline.InferenceExtension{
		InputTokens:      50,
		CacheReadTokens:  200,
		CacheWriteTokens: 100,
		OutputTokens:     25,
		ReasoningTokens:  0,
		PromptTokens:     350, // 50 + 200 + 100
		CompletionTokens: 25,
		TotalTokens:      375,
	})
}

// Non-beta SSE: prompt counts on message_start, output on message_delta.
func TestSplitTokens_AnthropicSSE_NonBeta(t *testing.T) {
	ext := &pipeline.InferenceExtension{Model: "claude-opus-4-8"}
	body := []byte("data: {\"type\":\"message_start\",\"message\":{\"usage\":{\"input_tokens\":40,\"cache_creation_input_tokens\":10,\"cache_read_input_tokens\":30,\"output_tokens\":0}}}\n" +
		"data: {\"type\":\"content_block_delta\",\"delta\":{\"type\":\"text_delta\",\"text\":\"hi\"}}\n" +
		"data: {\"type\":\"message_delta\",\"delta\":{\"stop_reason\":\"end_turn\"},\"usage\":{\"output_tokens\":12}}\n")
	parseAnthropicSSE(body, ext)

	checkSplit(t, ext, pipeline.InferenceExtension{
		InputTokens:      40,
		CacheReadTokens:  30,
		CacheWriteTokens: 10,
		OutputTokens:     12,
		ReasoningTokens:  0,
		PromptTokens:     80,
		CompletionTokens: 12,
		TotalTokens:      92,
	})
}

// ?beta=true SSE: message_start carries only input_tokens; cache counts
// arrive on message_delta. Exercises mergeAnthropicPromptMaxSeen.
func TestSplitTokens_AnthropicSSE_Beta(t *testing.T) {
	ext := &pipeline.InferenceExtension{Model: "claude-opus-4-8"}
	body := []byte("data: {\"type\":\"message_start\",\"message\":{\"usage\":{\"input_tokens\":9,\"output_tokens\":0}}}\n" +
		"data: {\"type\":\"content_block_delta\",\"delta\":{\"type\":\"text_delta\",\"text\":\"ok\"}}\n" +
		"data: {\"type\":\"message_delta\",\"delta\":{\"stop_reason\":\"end_turn\"},\"usage\":{\"input_tokens\":9,\"cache_creation_input_tokens\":1234,\"cache_read_input_tokens\":32529,\"output_tokens\":399}}\n")
	parseAnthropicSSE(body, ext)

	checkSplit(t, ext, pipeline.InferenceExtension{
		InputTokens:      9,
		CacheReadTokens:  32529,
		CacheWriteTokens: 1234,
		OutputTokens:     399,
		ReasoningTokens:  0,
		PromptTokens:     33772,
		CompletionTokens: 399,
		TotalTokens:      34171,
	})
}

// OpenAI JSON: inclusive-prompt normalization (Input = prompt - cached)
// and reasoning pulled from completion_tokens_details.
func TestSplitTokens_OpenAIJSON(t *testing.T) {
	ext := &pipeline.InferenceExtension{Model: "gpt-4o"}
	body := []byte(`{
		"choices": [{"message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
		"usage": {
			"prompt_tokens": 1500,
			"completion_tokens": 400,
			"total_tokens": 1900,
			"prompt_tokens_details": {"cached_tokens": 200},
			"completion_tokens_details": {"reasoning_tokens": 50}
		}
	}`)
	parseInferenceJSON(body, ext)

	checkSplit(t, ext, pipeline.InferenceExtension{
		InputTokens:      1300,
		CacheReadTokens:  200,
		CacheWriteTokens: 0,
		OutputTokens:     400,
		ReasoningTokens:  50,
		PromptTokens:     1500,
		CompletionTokens: 400,
		TotalTokens:      1900,
	})
}

// Malformed OpenAI response where cached_tokens > prompt_tokens must
// clamp InputTokens to 0, not go negative.
func TestSplitTokens_OpenAIJSON_ClampNegativeInput(t *testing.T) {
	ext := &pipeline.InferenceExtension{Model: "gpt-4o"}
	body := []byte(`{
		"choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
		"usage": {
			"prompt_tokens": 100,
			"completion_tokens": 20,
			"total_tokens": 120,
			"prompt_tokens_details": {"cached_tokens": 250}
		}
	}`)
	parseInferenceJSON(body, ext)

	if ext.InputTokens != 0 {
		t.Errorf("InputTokens = %d, want 0 (clamped)", ext.InputTokens)
	}
	if ext.CacheReadTokens != 250 {
		t.Errorf("CacheReadTokens = %d, want 250", ext.CacheReadTokens)
	}
}

// TestSplitTokens_OpenAISSE covers the OpenAI streaming shape with
// stream_options.include_usage — the usage block arrives on the final
// chunk and must map onto the neutral shape via the same normalization
// as the JSON path.
func TestSplitTokens_OpenAISSE(t *testing.T) {
	ext := &pipeline.InferenceExtension{Model: "gpt-4o", Stream: true}
	body := []byte("data: {\"choices\":[{\"delta\":{\"content\":\"hi\"}}]}\n" +
		"data: {\"choices\":[{\"delta\":{},\"finish_reason\":\"stop\"}],\"usage\":{\"prompt_tokens\":800,\"completion_tokens\":150,\"total_tokens\":950,\"prompt_tokens_details\":{\"cached_tokens\":600},\"completion_tokens_details\":{\"reasoning_tokens\":25}}}\n" +
		"data: [DONE]\n")
	parseInferenceSSE(body, ext)

	checkSplit(t, ext, pipeline.InferenceExtension{
		InputTokens:      200,
		CacheReadTokens:  600,
		CacheWriteTokens: 0,
		OutputTokens:     150,
		ReasoningTokens:  25,
		PromptTokens:     800,
		CompletionTokens: 150,
		TotalTokens:      950,
	})
}
