# Access Control Policy — baseline evaluation scenario

Grant access on a least-privilege basis. Only grant a (role, scope) pair when this
policy supports it; deny by default.

## Users → agent capabilities (inbound; user may call an agent)
- Developers may use both the source repository agent and the issue tracker agent.
- Testers may use only the issue tracker agent.

## Users → tool operations (outbound subject; user may reach a tool operation)
- Developers may read and write the source repository, and read the issue tracker.
- Testers may read and write the issue tracker.

## Agent roles → tool operations (outbound target; an agent role may reach a tool
## operation)
- The source repository agent's role may read and write the repository.
- The issue tracker agent's role may read and write the tracker.
