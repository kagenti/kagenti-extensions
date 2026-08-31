# Access Control Policy — ambiguous-clause evaluation scenario

Grant access on a least-privilege basis. Only grant a (role, scope) pair when this
policy supports it; deny by default.

## Users → agent capabilities (inbound; user may call an agent)
- Advisors may use an agent's current-status lookup capability.

## Users → tool operations (outbound subject; user may reach a tool operation)
- Advisors may access a subject's record information for advisory purposes. For
  advisory purposes, "record information" means a subject's current status only.

## Agent roles → tool operations (outbound target; an agent role may reach a tool
## operation)
- Advisory agents may look up a subject's current status and historical record.
