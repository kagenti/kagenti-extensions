package inferenceparser

import (
	"bytes"
	"encoding/json"
	"strings"

	"github.com/rossoctl/cortex/authbridge/authlib/pipeline"
)

// anthropicMessagesPath is the Anthropic Messages API endpoint. Clients
// (e.g. claude-code via a LiteLLM/Anthropic-compatible gateway) POST here
// instead of the OpenAI /v1/chat/completions endpoint, so the parser must
// recognize both dialects.
const anthropicMessagesPath = "/v1/messages"

// --- request ---

// anthropicRequest is the subset of the Anthropic Messages request we surface.
// Unlike OpenAI, the system prompt is a top-level field (string or text-block
// array), not a message with role "system".
type anthropicRequest struct {
	Model       string                `json:"model"`
	Messages    []anthropicReqMessage `json:"messages"`
	System      json.RawMessage       `json:"system"`
	Temperature *float64              `json:"temperature"`
	MaxTokens   *int                  `json:"max_tokens"`
	TopP        *float64              `json:"top_p"`
	Stream      bool                  `json:"stream"`
	Tools       []anthropicTool       `json:"tools"`
	ToolChoice  any                   `json:"tool_choice"`
}

// anthropicTool is an Anthropic tool definition. The schema lives under
// input_schema (vs OpenAI's nested function.parameters).
type anthropicTool struct {
	Name        string          `json:"name"`
	Description string          `json:"description"`
	InputSchema json.RawMessage `json:"input_schema"`
}

// anthropicReqMessage flattens the request message content to text. Anthropic
// content is a string or an array of content blocks (text / image / tool_use /
// tool_result); reuse flattenContent, which keeps text blocks and drops the
// rest — the same {"type":"text","text":...} shape OpenAI uses.
//
// ContentBytes records the size of what was there before that reduction, so
// the blocks flattenContent drops are still accounted for. In an agent loop
// the dropped blocks are the bulk of the conversation: every tool result comes
// back as a tool_result block, so a turn that reads a large file shows up as an
// empty Content and would otherwise look free.
type anthropicReqMessage struct {
	Role         string
	Content      string
	ContentBytes int
}

func (m *anthropicReqMessage) UnmarshalJSON(data []byte) error {
	var raw struct {
		Role    string          `json:"role"`
		Content json.RawMessage `json:"content"`
	}
	if err := json.Unmarshal(data, &raw); err != nil {
		return err
	}
	m.Role = raw.Role
	m.Content = flattenContent(raw.Content)
	m.ContentBytes = contentBytes(raw.Content)
	return nil
}

// parseAnthropicRequest builds an InferenceExtension from an Anthropic Messages
// request body. Returns nil for an empty or non-JSON body (caller treats nil as
// "not an inference request we can parse" and continues).
func parseAnthropicRequest(body []byte) *pipeline.InferenceExtension {
	if len(body) == 0 {
		return nil
	}
	var req anthropicRequest
	if err := json.Unmarshal(body, &req); err != nil {
		return nil
	}

	ext := &pipeline.InferenceExtension{
		Model:       req.Model,
		Temperature: req.Temperature,
		MaxTokens:   req.MaxTokens,
		TopP:        req.TopP,
		Stream:      req.Stream,
		ToolChoice:  req.ToolChoice,
		// Every populated InferenceExtension is an outbound LLM call — an
		// agent action. Same classification as the OpenAI path.
		IsAction: true,
	}

	// Anthropic carries the system prompt top-level, not as a message role.
	// Surface it as a leading system message so downstream policy plugins
	// (IBAC, etc.) see it the same way they see OpenAI's system message.
	if sys := flattenContent(req.System); sys != "" {
		ext.Messages = append(ext.Messages, pipeline.InferenceMessage{
			Role: "system", Content: sys, ContentBytes: contentBytes(req.System),
		})
	}
	for _, msg := range req.Messages {
		ext.Messages = append(ext.Messages, pipeline.InferenceMessage{
			Role: msg.Role, Content: msg.Content, ContentBytes: msg.ContentBytes,
		})
	}
	for _, tool := range req.Tools {
		if tool.Name == "" {
			continue
		}
		ext.Tools = append(ext.Tools, pipeline.InferenceTool{
			Name:        tool.Name,
			Description: tool.Description,
			Parameters:  rawMessageToMap(tool.InputSchema),
		})
	}
	return ext
}

// --- usage (shared by response + streaming) ---

// anthropicUsage mirrors the Messages API usage block. The true input size is
// input_tokens + cache_creation_input_tokens + cache_read_input_tokens (cached
// context still counts as input); promptTotal sums them.
type anthropicUsage struct {
	InputTokens              int `json:"input_tokens"`
	OutputTokens             int `json:"output_tokens"`
	CacheCreationInputTokens int `json:"cache_creation_input_tokens"`
	CacheReadInputTokens     int `json:"cache_read_input_tokens"`
}

func (u anthropicUsage) promptTotal() int {
	return u.InputTokens + u.CacheCreationInputTokens + u.CacheReadInputTokens
}

// --- non-streaming response ---

type anthropicResponse struct {
	Content    []anthropicContentBlock `json:"content"`
	StopReason string                  `json:"stop_reason"`
	Usage      anthropicUsage          `json:"usage"`
}

type anthropicContentBlock struct {
	Type  string          `json:"type"`
	Text  string          `json:"text"`
	ID    string          `json:"id"`
	Name  string          `json:"name"`
	Input json.RawMessage `json:"input"`
}

// parseAnthropicJSON parses a non-streaming Messages response: text blocks ->
// completion, tool_use blocks -> tool calls, usage -> token counts.
func parseAnthropicJSON(body []byte, ext *pipeline.InferenceExtension) {
	var resp anthropicResponse
	if err := json.Unmarshal(body, &resp); err != nil {
		return
	}
	var b strings.Builder
	for _, blk := range resp.Content {
		switch blk.Type {
		case "text":
			if blk.Text != "" {
				if b.Len() > 0 {
					b.WriteByte('\n')
				}
				b.WriteString(blk.Text)
			}
		case "tool_use":
			ext.ToolCalls = append(ext.ToolCalls, pipeline.InferenceToolCall{
				ID:        blk.ID,
				Name:      blk.Name,
				Arguments: string(blk.Input),
			})
		}
	}
	ext.Completion = b.String()
	if resp.StopReason != "" {
		ext.FinishReason = resp.StopReason
	}
	ext.PromptTokens = resp.Usage.promptTotal()
	ext.CompletionTokens = resp.Usage.OutputTokens
	ext.TotalTokens = ext.PromptTokens + ext.CompletionTokens
	ext.CacheWriteTokens = resp.Usage.CacheCreationInputTokens
	ext.CacheReadTokens = resp.Usage.CacheReadInputTokens
}

// --- streaming ---

// anthropicStreamEvent is one SSE event's data payload. The Messages stream is
// a sequence of typed events (vs OpenAI's uniform chat.completion.chunk):
// message_start (carries usage — but see below), content_block_delta (text_delta /
// input_json_delta / thinking_delta), message_delta (delta.stop_reason +
// cumulative usage.output_tokens, and on the ?beta=true path the prompt-cache
// counts too), message_stop, plus ping/content_block_*.
type anthropicStreamEvent struct {
	Type    string `json:"type"`
	Message *struct {
		Usage anthropicUsage `json:"usage"`
	} `json:"message"`
	Delta *struct {
		Type       string `json:"type"`
		Text       string `json:"text"`
		StopReason string `json:"stop_reason"`
		// PartialJSON carries a fragment of a tool call's arguments on an
		// input_json_delta. The model streams tool arguments as text that
		// is only valid JSON once every fragment is concatenated.
		PartialJSON string `json:"partial_json"`
	} `json:"delta"`
	Usage *anthropicUsage `json:"usage"`

	// Index identifies which content block an event belongs to. A response
	// may contain several blocks (text plus one or more tool calls), and
	// their deltas are only distinguishable by this index.
	Index *int `json:"index"`
	// ContentBlock is the opening descriptor on a content_block_start. For a
	// tool call it carries the id and name; the arguments follow as deltas.
	ContentBlock *struct {
		Type string `json:"type"`
		ID   string `json:"id"`
		Name string `json:"name"`
	} `json:"content_block"`
}

// anthropicToolCallState accumulates one streamed tool call. The id and name
// arrive on content_block_start; the arguments follow as a series of
// input_json_delta fragments that are only valid JSON once concatenated.
type anthropicToolCallState struct {
	id   string
	name string
	args strings.Builder
}

// openAnthropicTool starts accumulating a tool call for content block index.
// The entry is pointer-held: a strings.Builder must not be copied once used,
// which a value slice would do the moment append reallocates.
func (s *inferenceStreamState) openAnthropicTool(index *int, id, name string) {
	tc := &anthropicToolCallState{id: id, name: name}
	s.toolCalls = append(s.toolCalls, tc)
	if index != nil {
		if s.toolsByIndex == nil {
			s.toolsByIndex = map[int]*anthropicToolCallState{}
		}
		s.toolsByIndex[*index] = tc
	}
	s.openTool = tc
}

// anthropicTool resolves the tool call a delta belongs to. Nil index falls
// back to the most recently opened call — blocks are emitted sequentially,
// so that is the same call the index would have named.
func (s *inferenceStreamState) anthropicTool(index *int) *anthropicToolCallState {
	if index != nil {
		if tc, ok := s.toolsByIndex[*index]; ok {
			return tc
		}
		// An indexed delta for a block we never saw open is a text block's
		// delta or a shape we don't model — not the open tool's arguments.
		return nil
	}
	return s.openTool
}

// closeAnthropicTool drops the fallback target at a content_block_stop, so a
// later unindexed delta can't append to a call that already ended.
func (s *inferenceStreamState) closeAnthropicTool() {
	s.openTool = nil
}

// totalAnthropicUsage derives the running total from the parts it was given.
// It runs after every usage update rather than only when output tokens arrive,
// because TotalTokens is the gate finalize uses to decide whether any count is
// worth recording — so leaving it at zero discards a prompt size already known.
// Two streams hit that: a terminal message_delta reporting the prompt with
// output_tokens == 0, and a turn the caller interrupted after message_start,
// which never reaches a message_delta at all. Both were billed for the prompt.
func (s *inferenceStreamState) totalAnthropicUsage() {
	s.usage.TotalTokens = s.usage.PromptTokens + s.usage.CompletionTokens
}

// foldAnthropicFrame folds one Messages SSE event into the running stream state.
// The prompt size is taken as the largest total seen, because different Messages
// API paths report it on different events: message_start on the plain path,
// message_delta on the ?beta=true path. The completion accumulates from
// text_delta blocks; tool calls accumulate from content_block_start plus
// input_json_delta; stop_reason and the cumulative output_tokens arrive in
// message_delta. Unknown events (ping, message_stop) are ignored.
func foldAnthropicFrame(frame []byte, state *inferenceStreamState, ext *pipeline.InferenceExtension) {
	var ev anthropicStreamEvent
	if err := json.Unmarshal(frame, &ev); err != nil {
		return
	}
	switch ev.Type {
	case "message_start":
		if ev.Message != nil {
			state.usage.PromptTokens = ev.Message.Usage.promptTotal()
			state.usage.CacheWriteTokens = ev.Message.Usage.CacheCreationInputTokens
			state.usage.CacheReadTokens = ev.Message.Usage.CacheReadInputTokens
			state.totalAnthropicUsage()
		}
	case "content_block_start":
		// A tool call opens here and is populated by later deltas. Text
		// blocks need no setup — their deltas append to the completion.
		if ev.ContentBlock != nil && ev.ContentBlock.Type == "tool_use" {
			state.openAnthropicTool(ev.Index, ev.ContentBlock.ID, ev.ContentBlock.Name)
		}
	case "content_block_delta":
		if ev.Delta == nil {
			return
		}
		switch ev.Delta.Type {
		case "text_delta":
			state.completion.WriteString(ev.Delta.Text)
		case "input_json_delta":
			if tc := state.anthropicTool(ev.Index); tc != nil {
				tc.args.WriteString(ev.Delta.PartialJSON)
			}
		}
	case "content_block_stop":
		state.closeAnthropicTool()
	case "message_delta":
		if ev.Delta != nil && ev.Delta.StopReason != "" {
			ext.FinishReason = ev.Delta.StopReason
		}
		if ev.Usage != nil {
			// The prompt side can arrive here rather than in message_start.
			// Clients using the ?beta=true Messages path (Claude Code sends
			// anthropic-beta: claude-code-*) get a message_start carrying only
			// input_tokens, with cache_creation_input_tokens and
			// cache_read_input_tokens deferred to message_delta — so reading
			// the prompt size from message_start alone undercounts a cached
			// agent request by orders of magnitude (a 33k-token turn recorded
			// as 9). Take the larger value: on the non-beta path message_delta
			// carries no input counts, and assigning unconditionally would
			// clobber the correct message_start total with zero.
			if p := ev.Usage.promptTotal(); p > state.usage.PromptTokens {
				state.usage.PromptTokens = p
				// Keep the split consistent with whichever usage block
				// won the total, so the parts always sum into it.
				state.usage.CacheWriteTokens = ev.Usage.CacheCreationInputTokens
				state.usage.CacheReadTokens = ev.Usage.CacheReadInputTokens
			}
			if ev.Usage.OutputTokens > 0 {
				// usage.output_tokens in message_delta is cumulative — take
				// the latest rather than accumulating.
				state.usage.CompletionTokens = ev.Usage.OutputTokens
			}
			state.totalAnthropicUsage()
		}
	}
}

// parseAnthropicSSE folds a fully-buffered Messages SSE body. Mirrors
// parseInferenceSSE for the legacy OnResponse path; the live listener uses
// foldAnthropicFrame via OnResponseFrame instead.
func parseAnthropicSSE(body []byte, ext *pipeline.InferenceExtension) {
	state := &inferenceStreamState{}
	for _, line := range bytes.Split(body, []byte("\n")) {
		line = bytes.TrimSpace(line)
		if !bytes.HasPrefix(line, []byte("data:")) {
			continue
		}
		data := bytes.TrimSpace(bytes.TrimPrefix(line, []byte("data:")))
		if len(data) == 0 {
			continue
		}
		foldAnthropicFrame(data, state, ext)
	}
	state.finalize(ext)
}

// rawMessageToMap decodes a JSON object into a map, returning nil for an absent
// or non-object value (so a non-object input_schema doesn't fail the parse).
func rawMessageToMap(raw json.RawMessage) map[string]any {
	if len(raw) == 0 {
		return nil
	}
	var m map[string]any
	if err := json.Unmarshal(raw, &m); err != nil {
		return nil
	}
	return m
}
