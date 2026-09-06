# Access Control Policy — unreachable-resources evaluation scenario

Grant access on a least-privilege basis. Only grant a (role, scope) pair when this
policy supports it; deny by default.

## Users → agent capabilities (inbound; user may call an agent)
- Clerks may use an intake agent.

## Users → tool operations (outbound subject; user may reach a tool operation)
- Clerks may read and write records.

## Agent roles → tool operations (outbound target; an agent role may reach a tool
## operation)
- The intake agent's role may read and write records.
