# Access Control Policy — Release Operations

Grant access on a least-privilege basis. Only grant a (role, scope) pair when this
policy supports it; deny by default.

## Users → agent capabilities (inbound; user may call an agent)
- support-rep may use desk-access.
- release-engineer may use release-access.
- release-coordinator may use release-access.
- content-editor may use release-access.

## Users → tool operations (outbound subject; user may reach a tool operation, or a
## capability delegated by one agent to another, through the agent it calls)
- support-rep may perform ticket-read and ticket-write.
- release-engineer may perform all deployment operations.
- release-coordinator may access deployment status information.
- content-editor may perform wiki-read and wiki-write.

## Agent roles → tool operations (outbound target; an agent role may reach a tool
## operation, or a capability delegated to it by another agent)
- desk_operations may perform ticket-read and ticket-write.
- release_operations may perform all deployment operations.
- content_operations may perform wiki-read and wiki-write.
