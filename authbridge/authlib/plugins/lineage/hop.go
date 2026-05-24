package lineage

import "github.com/kagenti/kagenti-extensions/authbridge/authlib/pipeline"

// HopKind classifies the semantic type of a data-lineage hop.
type HopKind string

const (
	HopPrincipalToAgent HopKind = "principal_to_agent"
	HopAgentToAgent     HopKind = "agent_to_agent"
	HopAgentToTool      HopKind = "agent_to_tool"
	HopAgentToLLM       HopKind = "agent_to_llm"
	HopAgentToService   HopKind = "agent_to_service"
)

type hopInfo struct {
	Kind     HopKind
	Protocol string
}

// determineHop classifies a pipeline context into a hop kind and protocol.
// Inbound requests are always principal_to_agent. Outbound requests are
// classified by which protocol parser (if any) populated Extensions.
func determineHop(pctx *pipeline.Context) hopInfo {
	if pctx.Direction == pipeline.Inbound {
		return hopInfo{Kind: HopPrincipalToAgent, Protocol: "http"}
	}
	switch {
	case pctx.Extensions.A2A != nil:
		return hopInfo{Kind: HopAgentToAgent, Protocol: "a2a"}
	case pctx.Extensions.MCP != nil:
		return hopInfo{Kind: HopAgentToTool, Protocol: "mcp"}
	case pctx.Extensions.Inference != nil:
		return hopInfo{Kind: HopAgentToLLM, Protocol: "inference"}
	default:
		return hopInfo{Kind: HopAgentToService, Protocol: "http"}
	}
}
