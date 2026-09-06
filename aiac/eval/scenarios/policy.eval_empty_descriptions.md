# Access Control Policy — empty-descriptions evaluation scenario

Grant access on a least-privilege basis. Only grant a (role, scope) pair when this
policy supports it; deny by default.

## Users → agent capabilities (inbound; user may call an agent)
- Operators may use a device-control agent.

## Users → tool operations (outbound subject; user may reach a tool operation)
- Operators may open and close a device.

## Agent roles → tool operations (outbound target; an agent role may reach a tool
## operation)
- The device-control agent's role may open and close the device.
