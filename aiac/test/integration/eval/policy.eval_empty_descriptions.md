# Access Control Policy — empty-descriptions evaluation scenario

Grant access on a least-privilege basis. Only grant a (role, scope) pair when this
policy supports it; deny by default.

## Users → agent capabilities (inbound; user may call an agent)
- field-operator may use irrigation-access.

## Users → tool operations (outbound subject; user may reach a tool operation)
- field-operator may perform valve-open and valve-close.

## Agent roles → tool operations (outbound target; an agent role may reach a tool
## operation)
- irrigation_operations may perform valve-open and valve-close.
