# Access Control Policy — unreachable-resources evaluation scenario (reworded)

Access should be granted sparingly: a (role, scope) pair is allowed only when this document says
so, and everything else is refused.

## Which agents each user may call (inbound)
- The clerk role can use an intake agent.

## Which tool operations each user may reach (outbound, subject side)
- Clerks are permitted to read and write records.

## Which tool operations each agent role may reach (outbound, target side)
- The intake agent's role covers reading and writing records.
