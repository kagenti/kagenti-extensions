# Access Control Policy — agent-to-agent delegation evaluation scenario

Grant access on a least-privilege basis. Only grant a (role, scope) pair when this
policy supports it; deny by default.

## Users → agent capabilities (inbound; user may call an agent)
- Coordinators may use a dispatch agent.
- Workers may use the same dispatch agent.

## Users → tool operations (outbound subject; user may reach a tool operation, or a capability delegated by one agent to another, through the agent it calls)
- Coordinators may read records, write records, and have a downstream step carried
  out on their behalf.
- Workers may read and write records.

## Agent roles → tool operations (outbound target; an agent role may reach a tool operation, or a capability delegated to it by another agent)
- The dispatch agent's role may read records, write records, and have the
  downstream step carried out.
