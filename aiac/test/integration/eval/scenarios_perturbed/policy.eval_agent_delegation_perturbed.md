# Access Control Policy — agent-to-agent delegation evaluation scenario (reworded)

Access should be granted sparingly: a (role, scope) pair is allowed only when this document says
so, and everything else is refused.

## Which agents each user may call (inbound)
- The shipment-coordinator role can use the dispatch-access capability.
- The dock-worker role can use the dispatch-access capability.

## Which tool operations each user may reach (outbound, subject side — including capabilities
## handed off from one agent to another through the agent a user calls)
- Shipment coordinators may read manifests, write manifests, and have customs clearance carried
  out on their behalf.
- Dock workers may read and write manifests.

## Which tool operations each agent role may reach (outbound, target side — including
## capabilities handed off to it by another agent)
- The dispatch_operations role covers reading manifests, writing manifests, and having customs
  clearance carried out.
