# Access Control Policy — unreachable-resources evaluation scenario

Grant access on a least-privilege basis. Only grant a (role, scope) pair when this
policy supports it; deny by default.

## Users → agent capabilities (inbound; user may call an agent)
- front-desk-clerk may use intake-access.

## Users → tool operations (outbound subject; user may reach a tool operation)
- front-desk-clerk may perform records-read and records-write.

## Agent roles → tool operations (outbound target; an agent role may reach a tool
## operation)
- intake_operations may perform records-read and records-write.
