# Access Control Policy — wildcard-grant evaluation scenario

Grant access on a least-privilege basis. Only grant a (role, scope) pair when this
policy supports it; deny by default.

## Users → agent capabilities (inbound; user may call an agent)
- Managers may use a resource agent.

## Users → tool operations (outbound subject; user may reach a tool operation)
- Managers are authorized to perform all resource operations: checking levels,
  adjusting counts, and placing orders.

## Agent roles → tool operations (outbound target; an agent role may reach a tool
## operation)
- The resource agent's role covers all resource operations: checking levels,
  adjusting counts, and placing orders.
