# Access Control Policy — agent-to-agent delegation evaluation scenario

Grant access on a least-privilege basis. Only grant a (role, scope) pair when this
policy supports it; deny by default.

## Users → agent capabilities (inbound; user may call an agent)
- shipment-coordinator may use dispatch-access.
- dock-worker may use dispatch-access.

## Users → tool operations (outbound subject; user may reach a tool operation, or a
## capability delegated by one agent to another, through the agent it calls)
- shipment-coordinator may perform manifest-read, manifest-write, and customs-clearance.
- dock-worker may perform manifest-read and manifest-write.

## Agent roles → tool operations (outbound target; an agent role may reach a tool
## operation, or a capability delegated to it by another agent)
- dispatch_operations may perform manifest-read, manifest-write, and customs-clearance.
