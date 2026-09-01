package inferenceparser

import (
	"context"
	"testing"

	"github.com/rossoctl/cortex/authbridge/authlib/pipeline"
)

func TestInferenceParser_AnthropicMessages_Request(t *testing.T) {
	p := NewInferenceParser()
	pctx := &pipeline.Context{
		Path: "/v1/messages",
		Body: []byte(`{
			"model": "claude-opus-4-8",
			"system": "You are a helpful assistant.",
			"messages": [
				{"role": "user", "content": "What is the weather in NYC?"}
			],
			"max_tokens": 1024,
			"temperature": 0.7,
			"stream": false,
			"tools": [
				{"name": "get_weather", "description": "Get weather", "input_schema": {"type": "object"}}
			]
		}`),
	}

	if action := p.OnRequest(context.Background(), pctx); action.Type != pipeline.Continue {
		t.Fatalf("expected Continue, got %v", action.Type)
	}
	ext := pctx.Extensions.Inference
	if ext == nil {
		t.Fatal("Extensions.Inference is nil — /v1/messages not parsed")
	}
	if ext.Model != "claude-opus-4-8" {
		t.Errorf("Model = %q, want claude-opus-4-8", ext.Model)
	}
	if !ext.IsAction {
		t.Error("IsAction should be true for an inference request")
	}
	// system (top-level) is surfaced as a leading system message, then the user turn.
	if len(ext.Messages) != 2 || ext.Messages[0].Role != "system" || ext.Messages[1].Role != "user" {
		t.Fatalf("Messages = %+v, want [system, user]", ext.Messages)
	}
	if ext.Messages[0].Content != "You are a helpful assistant." {
		t.Errorf("system content = %q", ext.Messages[0].Content)
	}
	if ext.MaxTokens == nil || *ext.MaxTokens != 1024 {
		t.Errorf("MaxTokens = %v, want 1024", ext.MaxTokens)
	}
	if len(ext.Tools) != 1 || ext.Tools[0].Name != "get_weather" {
		t.Fatalf("Tools = %+v, want [get_weather]", ext.Tools)
	}
}

func TestInferenceParser_AnthropicMessages_ContentBlockArray(t *testing.T) {
	// Anthropic content can be a block array; flatten text blocks like OpenAI.
	p := NewInferenceParser()
	pctx := &pipeline.Context{
		Path: "/v1/messages",
		Body: []byte(`{
			"model": "claude-opus-4-8",
			"max_tokens": 64,
			"messages": [
				{"role": "user", "content": [
					{"type": "text", "text": "part one"},
					{"type": "text", "text": "part two"}
				]}
			]
		}`),
	}
	p.OnRequest(context.Background(), pctx)
	ext := pctx.Extensions.Inference
	if ext == nil || len(ext.Messages) != 1 {
		t.Fatalf("ext/messages = %+v", ext)
	}
	if ext.Messages[0].Content != "part one\npart two" {
		t.Errorf("flattened content = %q, want \"part one\\npart two\"", ext.Messages[0].Content)
	}
}

func TestInferenceParser_AnthropicMessages_NonStreamingResponse(t *testing.T) {
	p := NewInferenceParser()
	pctx := &pipeline.Context{Path: "/v1/messages"}
	// non-streaming: ext.Stream == false → one-shot last=true frame is parsed as JSON.
	pctx.Extensions.Inference = &pipeline.InferenceExtension{Model: "claude-opus-4-8", IsAction: true}

	body := []byte(`{
		"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-4-8",
		"content": [
			{"type": "text", "text": "It is sunny."},
			{"type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {"city": "NYC"}}
		],
		"stop_reason": "tool_use",
		"usage": {"input_tokens": 25, "output_tokens": 8, "cache_read_input_tokens": 2}
	}`)
	p.OnResponseFrame(context.Background(), pctx, body, true)

	ext := pctx.Extensions.Inference
	if ext.Completion != "It is sunny." {
		t.Errorf("Completion = %q", ext.Completion)
	}
	if ext.FinishReason != "tool_use" {
		t.Errorf("FinishReason = %q, want tool_use", ext.FinishReason)
	}
	if len(ext.ToolCalls) != 1 || ext.ToolCalls[0].Name != "get_weather" {
		t.Fatalf("ToolCalls = %+v", ext.ToolCalls)
	}
	// PromptTokens = input_tokens + cache_read (25 + 2); CompletionTokens = output_tokens.
	if ext.PromptTokens != 27 || ext.CompletionTokens != 8 || ext.TotalTokens != 35 {
		t.Errorf("tokens = prompt %d / completion %d / total %d, want 27/8/35",
			ext.PromptTokens, ext.CompletionTokens, ext.TotalTokens)
	}
	// The cached share of the prompt is recorded separately: 2 read, no writes.
	if ext.CacheReadTokens != 2 || ext.CacheWriteTokens != 0 {
		t.Errorf("cache = write %d / read %d, want 0/2",
			ext.CacheWriteTokens, ext.CacheReadTokens)
	}
}

func TestInferenceParser_AnthropicMessages_StreamFoldsEvents(t *testing.T) {
	p := NewInferenceParser()
	pctx := &pipeline.Context{Path: "/v1/messages"}
	pctx.Extensions.Inference = &pipeline.InferenceExtension{Model: "claude-opus-4-8", Stream: true, IsAction: true}

	frames := [][]byte{
		[]byte(`{"type":"message_start","message":{"id":"msg_1","type":"message","role":"assistant","usage":{"input_tokens":25,"output_tokens":1}}}`),
		[]byte(`{"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}`),
		[]byte(`{"type":"ping"}`),
		[]byte(`{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hello"}}`),
		[]byte(`{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":" world"}}`),
		[]byte(`{"type":"content_block_stop","index":0}`),
		[]byte(`{"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"output_tokens":15}}`),
		[]byte(`{"type":"message_stop"}`),
	}
	for _, f := range frames {
		if action := p.OnResponseFrame(context.Background(), pctx, f, false); action.Type != pipeline.Continue {
			t.Fatalf("frame action = %v, want Continue", action.Type)
		}
	}
	// Mid-stream: not finalized yet.
	if pctx.Extensions.Inference.Completion != "" {
		t.Errorf("Completion populated mid-stream = %q", pctx.Extensions.Inference.Completion)
	}
	// Finalize.
	p.OnResponseFrame(context.Background(), pctx, nil, true)

	ext := pctx.Extensions.Inference
	if ext.Completion != "Hello world" {
		t.Errorf("Completion = %q, want \"Hello world\"", ext.Completion)
	}
	if ext.FinishReason != "end_turn" {
		t.Errorf("FinishReason = %q, want end_turn", ext.FinishReason)
	}
	// input_tokens from message_start; cumulative output_tokens from message_delta.
	if ext.PromptTokens != 25 || ext.CompletionTokens != 15 || ext.TotalTokens != 40 {
		t.Errorf("tokens = prompt %d / completion %d / total %d, want 25/15/40",
			ext.PromptTokens, ext.CompletionTokens, ext.TotalTokens)
	}
}

// TestInferenceParser_AnthropicMessages_StreamBetaPathUsage covers the ?beta=true
// Messages path, where the prompt-cache counts arrive in message_delta instead of
// message_start. The frames below are the usage blocks captured verbatim from a
// real Claude Code turn (anthropic-beta: claude-code-20250219) against an
// Anthropic-compatible gateway: message_start carried only input_tokens, and the
// 33,763 cached tokens appeared two events later. Reading the prompt size from
// message_start alone recorded that turn as 9 tokens instead of 33,772.
func TestInferenceParser_AnthropicMessages_StreamBetaPathUsage(t *testing.T) {
	p := NewInferenceParser()
	// Query-free Path: the HTTP listeners (forwardproxy, reverseproxy) populate
	// Context.Path from r.URL.Path, so /v1/messages?beta=true arrives here as
	// /v1/messages. extproc does NOT — it uses the :path pseudo-header, query
	// included; TestInferenceParser_AnthropicMessages_QueryStringPath covers that.
	pctx := &pipeline.Context{Path: "/v1/messages"}
	pctx.Extensions.Inference = &pipeline.InferenceExtension{Model: "claude-haiku-4-5", Stream: true, IsAction: true}

	frames := [][]byte{
		[]byte(`{"type":"message_start","message":{"id":"msg_bdrk_1","type":"message","role":"assistant","usage":{"input_tokens":9,"output_tokens":0}}}`),
		[]byte(`{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Done"}}`),
		[]byte(`{"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"input_tokens":9,"output_tokens":399,"cache_creation_input_tokens":3755,"cache_read_input_tokens":30008}}`),
		[]byte(`{"type":"message_stop"}`),
	}
	for _, f := range frames {
		p.OnResponseFrame(context.Background(), pctx, f, false)
	}
	p.OnResponseFrame(context.Background(), pctx, nil, true)

	ext := pctx.Extensions.Inference
	// 9 + 3755 + 30008 — the cached context is still billed input.
	if ext.PromptTokens != 33772 {
		t.Errorf("PromptTokens = %d, want 33772 (message_delta usage ignored?)", ext.PromptTokens)
	}
	if ext.CompletionTokens != 399 || ext.TotalTokens != 34171 {
		t.Errorf("tokens = completion %d / total %d, want 399/34171",
			ext.CompletionTokens, ext.TotalTokens)
	}
	if ext.FinishReason != "end_turn" {
		t.Errorf("FinishReason = %q, want end_turn", ext.FinishReason)
	}
	// The same event carries how the cached 33,763 split between writes and
	// reads. A write bills 1.25x base and a read 0.1x, so collapsing both into
	// PromptTokens leaves a 12.5x spread invisible: this turn cost roughly
	// eleven times what the same prompt costs once the entry is warm.
	if ext.CacheWriteTokens != 3755 || ext.CacheReadTokens != 30008 {
		t.Errorf("cache = write %d / read %d, want 3755/30008",
			ext.CacheWriteTokens, ext.CacheReadTokens)
	}
}

// TestInferenceParser_AnthropicMessages_StreamZeroOutputUsage covers a stream
// whose terminal message_delta reports the prompt with output_tokens == 0 — a
// refusal or an immediately-stopped generation. The prompt was billed, cache
// reads included, so the counts must survive: recomputing the total only inside
// the output_tokens > 0 arm left TotalTokens at zero, and finalize gates the
// whole usage copy on that total being non-zero, so a 5,109-token prompt landed
// on the extension as nothing at all.
func TestInferenceParser_AnthropicMessages_StreamZeroOutputUsage(t *testing.T) {
	p := NewInferenceParser()
	pctx := &pipeline.Context{Path: "/v1/messages"}
	pctx.Extensions.Inference = &pipeline.InferenceExtension{Model: "claude-haiku-4-5", Stream: true, IsAction: true}

	frames := [][]byte{
		[]byte(`{"type":"message_start","message":{"id":"msg_bdrk_2","type":"message","role":"assistant","usage":{"input_tokens":9,"output_tokens":0}}}`),
		[]byte(`{"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"input_tokens":9,"output_tokens":0,"cache_creation_input_tokens":100,"cache_read_input_tokens":5000}}`),
		[]byte(`{"type":"message_stop"}`),
	}
	for _, f := range frames {
		p.OnResponseFrame(context.Background(), pctx, f, false)
	}
	p.OnResponseFrame(context.Background(), pctx, nil, true)

	ext := pctx.Extensions.Inference
	// 9 + 100 + 5000, all of it input the provider charged for.
	if ext.PromptTokens != 5109 || ext.TotalTokens != 5109 {
		t.Errorf("tokens = prompt %d / total %d, want 5109/5109 (usage discarded on the zero-output path?)",
			ext.PromptTokens, ext.TotalTokens)
	}
	if ext.CompletionTokens != 0 {
		t.Errorf("CompletionTokens = %d, want 0", ext.CompletionTokens)
	}
	if ext.CacheWriteTokens != 100 || ext.CacheReadTokens != 5000 {
		t.Errorf("cache = write %d / read %d, want 100/5000",
			ext.CacheWriteTokens, ext.CacheReadTokens)
	}
}

// TestInferenceParser_AnthropicMessages_StreamInterruptedAfterStart covers a
// stream the caller abandoned after message_start — the shape an agent produces
// every time a user cancels a running turn. No message_delta ever arrives, so
// message_start's input_tokens is the only usage the stream reported, and it is
// the one the provider billed. The turn previously recorded skip/no_response_body
// and no counts, making cancelled turns free in the accounting.
//
// Recovery is partial by construction: on the ?beta=true path the cache counts
// ride on message_delta, so an interrupted turn can only ever report the
// uncached input_tokens.
func TestInferenceParser_AnthropicMessages_StreamInterruptedAfterStart(t *testing.T) {
	p := NewInferenceParser()
	pctx := &pipeline.Context{Path: "/v1/messages"}
	pctx.Extensions.Inference = &pipeline.InferenceExtension{Model: "claude-haiku-4-5", Stream: true, IsAction: true}

	p.OnResponseFrame(context.Background(), pctx,
		[]byte(`{"type":"message_start","message":{"id":"msg_bdrk_3","type":"message","role":"assistant","usage":{"input_tokens":24000,"output_tokens":0}}}`), false)
	// The connection ends here: no message_delta, no message_stop.
	p.OnResponseFrame(context.Background(), pctx, nil, true)

	ext := pctx.Extensions.Inference
	if ext.PromptTokens != 24000 || ext.TotalTokens != 24000 {
		t.Errorf("tokens = prompt %d / total %d, want 24000/24000 (interrupted stream discarded?)",
			ext.PromptTokens, ext.TotalTokens)
	}
	if ext.CompletionTokens != 0 {
		t.Errorf("CompletionTokens = %d, want 0", ext.CompletionTokens)
	}
	// Having counts to record, the response must be a real row rather than the
	// skip that stands in for a stream that reported nothing. Direction's zero
	// value is Inbound, so this context's invocations land on the inbound list.
	if invs := pctx.Extensions.Invocations; invs != nil {
		for _, inv := range invs.Inbound {
			if inv.Reason == "no_response_body" {
				t.Errorf("recorded %s/%s, want a real response row", inv.Action, inv.Reason)
			}
		}
	}
}

// TestInferenceParser_AnthropicMessages_StreamToolUse covers a streamed tool
// call. The pieces arrive across three event types — id and name on
// content_block_start, arguments as input_json_delta fragments that are only
// valid JSON once concatenated — so no single frame carries the call. Before
// this was folded in, a streaming turn recorded finishReason "tool_use" with an
// empty toolCalls list, while the equivalent non-streaming response recorded
// the call in full.
func TestInferenceParser_AnthropicMessages_StreamToolUse(t *testing.T) {
	p := NewInferenceParser()
	pctx := &pipeline.Context{Path: "/v1/messages"}
	pctx.Extensions.Inference = &pipeline.InferenceExtension{Model: "claude-haiku-4-5", Stream: true, IsAction: true}

	frames := [][]byte{
		[]byte(`{"type":"message_start","message":{"usage":{"input_tokens":12,"output_tokens":1}}}`),
		[]byte(`{"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}`),
		[]byte(`{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Checking."}}`),
		[]byte(`{"type":"content_block_stop","index":0}`),
		[]byte(`{"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"toolu_1","name":"Read","input":{}}}`),
		[]byte(`{"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"{\"file_pa"}}`),
		[]byte(`{"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"th\":\"/etc/hosts\"}"}}`),
		[]byte(`{"type":"content_block_stop","index":1}`),
		[]byte(`{"type":"message_delta","delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":40}}`),
		[]byte(`{"type":"message_stop"}`),
	}
	for _, f := range frames {
		p.OnResponseFrame(context.Background(), pctx, f, false)
	}
	p.OnResponseFrame(context.Background(), pctx, nil, true)

	ext := pctx.Extensions.Inference
	if len(ext.ToolCalls) != 1 {
		t.Fatalf("ToolCalls = %+v, want one call", ext.ToolCalls)
	}
	tc := ext.ToolCalls[0]
	if tc.ID != "toolu_1" || tc.Name != "Read" {
		t.Errorf("tool call id/name = %q/%q, want toolu_1/Read", tc.ID, tc.Name)
	}
	// The two partial_json fragments concatenate into the complete arguments.
	if tc.Arguments != `{"file_path":"/etc/hosts"}` {
		t.Errorf("Arguments = %q, want {\"file_path\":\"/etc/hosts\"}", tc.Arguments)
	}
	// The text block is unaffected — only text_delta feeds the completion.
	if ext.Completion != "Checking." {
		t.Errorf("Completion = %q, want \"Checking.\"", ext.Completion)
	}
	if ext.FinishReason != "tool_use" {
		t.Errorf("FinishReason = %q, want tool_use", ext.FinishReason)
	}
}

// TestInferenceParser_AnthropicMessages_StreamToolUseInterleaved proves the
// fragments are routed by block index rather than by arrival order. The two
// calls' deltas alternate here, which is the shape that would silently
// concatenate one call's arguments into the other if index were ignored.
func TestInferenceParser_AnthropicMessages_StreamToolUseInterleaved(t *testing.T) {
	p := NewInferenceParser()
	pctx := &pipeline.Context{Path: "/v1/messages"}
	pctx.Extensions.Inference = &pipeline.InferenceExtension{Model: "claude-haiku-4-5", Stream: true, IsAction: true}

	frames := [][]byte{
		[]byte(`{"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"toolu_a","name":"Bash","input":{}}}`),
		[]byte(`{"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"toolu_b","name":"Grep","input":{}}}`),
		[]byte(`{"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{\"cmd\":"}}`),
		[]byte(`{"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"{\"pattern\":"}}`),
		[]byte(`{"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"\"ls\"}"}}`),
		[]byte(`{"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"\"TODO\"}"}}`),
		[]byte(`{"type":"message_delta","delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":60}}`),
	}
	for _, f := range frames {
		p.OnResponseFrame(context.Background(), pctx, f, false)
	}
	p.OnResponseFrame(context.Background(), pctx, nil, true)

	ext := pctx.Extensions.Inference
	if len(ext.ToolCalls) != 2 {
		t.Fatalf("ToolCalls = %+v, want two calls", ext.ToolCalls)
	}
	// Order follows the opening frames, not the delta ordering.
	if ext.ToolCalls[0].Name != "Bash" || ext.ToolCalls[1].Name != "Grep" {
		t.Errorf("names = %q/%q, want Bash/Grep", ext.ToolCalls[0].Name, ext.ToolCalls[1].Name)
	}
	if ext.ToolCalls[0].Arguments != `{"cmd":"ls"}` {
		t.Errorf("Bash Arguments = %q, want {\"cmd\":\"ls\"}", ext.ToolCalls[0].Arguments)
	}
	if ext.ToolCalls[1].Arguments != `{"pattern":"TODO"}` {
		t.Errorf("Grep Arguments = %q, want {\"pattern\":\"TODO\"}", ext.ToolCalls[1].Arguments)
	}
}

// TestInferenceParser_AnthropicMessages_StreamToolUseOnlyIsNotASkip pins the
// finalize guard against the one stream shape where every other signal is
// absent: a turn cancelled while the model was still emitting tool arguments.
// There is no completion text, no stop_reason, and no usage block, so the
// guard's other three terms all hold — and recording a skip here would label
// a stream that carried a tool call as having had no response body, hiding it
// from any timeline filtered on observe.
//
// A real Anthropic stream opens with message_start, whose input_tokens keeps
// TotalTokens non-zero, so this is a latent case rather than a live one. It is
// also what makes the interleaved test above fragile: drop its trailing
// message_delta and the two calls it proves are captured would vanish into a
// skip.
func TestInferenceParser_AnthropicMessages_StreamToolUseOnlyIsNotASkip(t *testing.T) {
	p := NewInferenceParser()
	pctx := &pipeline.Context{Path: "/v1/messages"}
	pctx.Extensions.Inference = &pipeline.InferenceExtension{Model: "claude-haiku-4-5", Stream: true, IsAction: true}

	frames := [][]byte{
		[]byte(`{"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"toolu_x","name":"Read","input":{}}}`),
		[]byte(`{"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{\"file\":"}}`),
	}
	for _, f := range frames {
		p.OnResponseFrame(context.Background(), pctx, f, false)
	}
	p.OnResponseFrame(context.Background(), pctx, nil, true)

	ext := pctx.Extensions.Inference
	// Preconditions: this is the shape the guard's other three terms match.
	if ext.Completion != "" || ext.FinishReason != "" || ext.TotalTokens != 0 {
		t.Fatalf("fixture no longer exercises the guard: Completion=%q FinishReason=%q TotalTokens=%d",
			ext.Completion, ext.FinishReason, ext.TotalTokens)
	}
	if len(ext.ToolCalls) != 1 || ext.ToolCalls[0].Name != "Read" {
		t.Fatalf("ToolCalls = %+v, want one Read call", ext.ToolCalls)
	}
	// Arguments stay as the model left them — truncated, not discarded.
	if ext.ToolCalls[0].Arguments != `{"file":` {
		t.Errorf("Arguments = %q, want the partial fragment", ext.ToolCalls[0].Arguments)
	}

	inv := pctx.Extensions.Invocations
	if inv == nil || len(inv.Inbound) == 0 {
		t.Fatalf("Invocations = %+v, want a recorded response row", inv)
	}
	last := inv.Inbound[len(inv.Inbound)-1]
	if last.Action != pipeline.ActionObserve {
		t.Errorf("action = %s/%s, want observe (a captured tool call is a response body)",
			last.Action, last.Reason)
	}
}

// TestInferenceParser_AnthropicMessages_RequestContentBytes covers the sizes of
// messages the text flattening discards. In an agent loop those are most of the
// conversation: a tool_result block flattens to "" and reads as free, while the
// model was billed for every byte of it.
func TestInferenceParser_AnthropicMessages_RequestContentBytes(t *testing.T) {
	p := NewInferenceParser()
	pctx := &pipeline.Context{
		Path: "/v1/messages",
		Body: []byte(`{
			"model": "claude-haiku-4-5",
			"max_tokens": 64,
			"system": [{"type": "text", "text": "You are Claude Code."}],
			"messages": [
				{"role": "user", "content": "read /etc/hosts"},
				{"role": "assistant", "content": [
					{"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {"file_path": "/etc/hosts"}}
				]},
				{"role": "user", "content": [
					{"type": "tool_result", "tool_use_id": "toolu_1", "content": "127.0.0.1 localhost"}
				]}
			]
		}`),
	}
	p.OnRequest(context.Background(), pctx)

	ext := pctx.Extensions.Inference
	if ext == nil || len(ext.Messages) != 4 {
		t.Fatalf("Messages = %+v, want [system, user, assistant, user]", ext)
	}
	for i, m := range ext.Messages {
		if m.ContentBytes <= 0 {
			t.Errorf("Messages[%d] (%s) ContentBytes = %d, want > 0", i, m.Role, m.ContentBytes)
		}
	}
	// The two block-array messages carry no text, so Content is empty while
	// ContentBytes still reports what the request spent on them.
	for _, i := range []int{2, 3} {
		if ext.Messages[i].Content != "" {
			t.Errorf("Messages[%d] Content = %q, want empty (no text blocks)", i, ext.Messages[i].Content)
		}
	}
	// The tool_result payload is the larger of the two — a real one is a whole
	// file, which is exactly the cost this field exists to make visible.
	if ext.Messages[3].ContentBytes <= ext.Messages[1].ContentBytes {
		t.Errorf("tool_result ContentBytes (%d) should exceed the plain user turn (%d)",
			ext.Messages[3].ContentBytes, ext.Messages[1].ContentBytes)
	}
}

// TestInferenceParser_AnthropicMessages_QueryStringPath pins dialect dispatch
// when Context.Path carries a query string. extproc populates Path from the
// HTTP/2 :path pseudo-header, which includes the query, so Claude Code's
// POST /v1/messages?beta=true arrives here as "/v1/messages?beta=true" — while
// the HTTP listeners strip it via r.URL.Path.
//
// Two distinct failure modes are covered, both previously silent:
//
//   - OnRequest's exact-match switch fell to default, leaving
//     Extensions.Inference nil so the whole exchange went unrecorded;
//   - had dispatch matched but the four dialect-selection sites not been
//     normalised, an Anthropic stream would have been folded by the OpenAI
//     handler, which does not understand message_delta and would report zero
//     tokens rather than fail.
//
// Asserting the token counts therefore checks the routing, not just the match.
func TestInferenceParser_AnthropicMessages_QueryStringPath(t *testing.T) {
	p := NewInferenceParser()
	pctx := &pipeline.Context{
		Path: "/v1/messages?beta=true",
		Body: []byte(`{"model":"claude-haiku-4-5","messages":[{"role":"user","content":"hi"}],"stream":true}`),
	}

	if action := p.OnRequest(context.Background(), pctx); action.Type != pipeline.Continue {
		t.Fatalf("expected Continue, got %v", action.Type)
	}
	ext := pctx.Extensions.Inference
	if ext == nil {
		t.Fatal("Extensions.Inference is nil — query string defeated the dispatch switch")
	}
	if ext.Model != "claude-haiku-4-5" {
		t.Errorf("Model = %q, want claude-haiku-4-5", ext.Model)
	}

	frames := [][]byte{
		[]byte(`{"type":"message_start","message":{"id":"msg_q1","type":"message","role":"assistant","usage":{"input_tokens":11,"output_tokens":0}}}`),
		[]byte(`{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"ok"}}`),
		[]byte(`{"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"input_tokens":11,"output_tokens":7,"cache_read_input_tokens":500}}`),
	}
	for _, f := range frames {
		p.OnResponseFrame(context.Background(), pctx, f, false)
	}
	p.OnResponseFrame(context.Background(), pctx, nil, true)

	// 11 + 500 cached input; the OpenAI folder would leave these at 0.
	if ext.PromptTokens != 511 || ext.CompletionTokens != 7 || ext.TotalTokens != 518 {
		t.Errorf("tokens = prompt %d / completion %d / total %d, want 511/7/518 (wrong dialect?)",
			ext.PromptTokens, ext.CompletionTokens, ext.TotalTokens)
	}
	if ext.Completion != "ok" {
		t.Errorf("Completion = %q, want \"ok\"", ext.Completion)
	}
	if ext.FinishReason != "end_turn" {
		t.Errorf("FinishReason = %q, want end_turn", ext.FinishReason)
	}
}
