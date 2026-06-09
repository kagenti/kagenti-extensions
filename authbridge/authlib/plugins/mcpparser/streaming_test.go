package mcpparser

import (
	"context"
	"testing"

	"github.com/kagenti/kagenti-extensions/authbridge/authlib/pipeline"
)

func TestMCPParser_OnResponseFrame_PerMessageRecording(t *testing.T) {
	p := NewMCPParser()
	pctx := &pipeline.Context{
		Extensions: pipeline.Extensions{
			MCP: &pipeline.MCPExtension{Method: "tools/call"},
		},
	}

	// First frame: a complete JSON-RPC result.
	frame := []byte(`{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"hello"}]}}`)
	action := p.OnResponseFrame(context.Background(), pctx, frame, false)
	if action.Type != pipeline.Continue {
		t.Fatalf("action = %v, want Continue", action.Type)
	}
	if pctx.Extensions.MCP.Result == nil {
		t.Fatal("Result not populated by OnResponseFrame")
	}
	// last=true with empty frame finalizes (no-op for already-populated Result).
	p.OnResponseFrame(context.Background(), pctx, nil, true)
	if pctx.Extensions.MCP.Result == nil {
		t.Error("Result cleared on last=true")
	}
}

func TestMCPParser_OnResponseFrame_ApplicationJSONOneShot(t *testing.T) {
	p := NewMCPParser()
	pctx := &pipeline.Context{
		Extensions: pipeline.Extensions{
			MCP: &pipeline.MCPExtension{Method: "tools/call"},
		},
	}
	body := []byte(`{"jsonrpc":"2.0","id":2,"result":{"ok":true}}`)
	// application/json: single last=true frame containing whole body.
	action := p.OnResponseFrame(context.Background(), pctx, body, true)
	if action.Type != pipeline.Continue {
		t.Fatalf("action = %v, want Continue", action.Type)
	}
	if pctx.Extensions.MCP.Result == nil {
		t.Fatal("Result not populated")
	}
	if pctx.Extensions.MCP.Result["ok"] != true {
		t.Errorf("Result[ok] = %v, want true", pctx.Extensions.MCP.Result["ok"])
	}
}

func TestMCPParser_OnResponseFrame_ErrorFrame(t *testing.T) {
	p := NewMCPParser()
	pctx := &pipeline.Context{
		Extensions: pipeline.Extensions{
			MCP: &pipeline.MCPExtension{Method: "tools/call"},
		},
	}
	frame := []byte(`{"jsonrpc":"2.0","id":3,"error":{"code":-32601,"message":"method not found"}}`)
	p.OnResponseFrame(context.Background(), pctx, frame, true)
	if pctx.Extensions.MCP.Err == nil {
		t.Fatal("Err not populated")
	}
	if pctx.Extensions.MCP.Err.Code != -32601 {
		t.Errorf("Err.Code = %d, want -32601", pctx.Extensions.MCP.Err.Code)
	}
}

func TestMCPParser_OnResponseFrame_NoExtensionMeansNoOp(t *testing.T) {
	p := NewMCPParser()
	pctx := &pipeline.Context{}
	action := p.OnResponseFrame(context.Background(), pctx, []byte(`{}`), true)
	if action.Type != pipeline.Continue {
		t.Errorf("action = %v, want Continue", action.Type)
	}
	if pctx.Extensions.MCP != nil {
		t.Error("MCPExtension created when request side never participated")
	}
}

func TestMCPParser_OnResponseFrame_EmptyStreamRecordsSkip(t *testing.T) {
	p := NewMCPParser()
	pctx := &pipeline.Context{
		Extensions: pipeline.Extensions{
			MCP: &pipeline.MCPExtension{Method: "tools/call"},
		},
	}
	// Streaming response with zero data frames: only the last=true call.
	pctx.SetCurrentPlugin("mcp-parser", pipeline.InvocationPhaseResponse)
	p.OnResponseFrame(context.Background(), pctx, nil, true)
	pctx.ClearCurrentPlugin()
	if pctx.Extensions.Invocations == nil {
		t.Fatal("no invocation recorded")
	}
}

func TestMCPParser_OnResponseFrame_MalformedFrameSkipped(t *testing.T) {
	p := NewMCPParser()
	pctx := &pipeline.Context{
		Extensions: pipeline.Extensions{
			MCP: &pipeline.MCPExtension{Method: "tools/call"},
		},
	}
	action := p.OnResponseFrame(context.Background(), pctx, []byte("not json"), false)
	if action.Type != pipeline.Continue {
		t.Errorf("action = %v, want Continue (malformed frame should skip silently)", action.Type)
	}
	if pctx.Extensions.MCP.Result != nil || pctx.Extensions.MCP.Err != nil {
		t.Error("malformed frame populated Result/Err")
	}
}
