# Access Control Policy — agent-to-agent delegation evaluation scenario (reworded)

Access should be granted sparingly: a (role, scope) pair is allowed only when this document says
so, and everything else is refused.

## Which agents each user may call (inbound)
- The coordinator role can use the dispatch agent.
- The worker role can use the same dispatch agent.

## Which tool operations each user may reach (outbound, subject side — including capabilities handed off from one agent to another through the agent a user calls)
- Coordinators may read records, write records, and have a downstream step carried out on their
  behalf.
- Workers may read and write records.

## Which tool operations each agent role may reach (outbound, target side — including capabilities handed off to it by another agent)
- The dispatch agent's role covers reading records, writing records, and having the downstream
  step carried out.
