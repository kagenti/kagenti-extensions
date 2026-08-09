# Access Control Policy — ambiguous-clause evaluation scenario

Grant access on a least-privilege basis. Only grant a (role, scope) pair when this
policy supports it; deny by default.

## Users → agent capabilities (inbound; user may call an agent)
- enrollment-advisor may use registrar-access.

## Users → tool operations (outbound subject; user may reach a tool operation)
- enrollment-advisor may access enrollment information for advising purposes.

## Agent roles → tool operations (outbound target; an agent role may reach a tool
## operation)
- registrar_operations may perform enrollment-status and enrollment-history.
