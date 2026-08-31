# Access Control Policy — ambiguous-clause evaluation scenario (reworded)

Access should be granted sparingly: a (role, scope) pair is allowed only when this document says
so, and everything else is refused.

## Which agents each user may call (inbound)
- The advisor role can use an agent's current-status lookup capability.

## Which tool operations each user may reach (outbound, subject side)
- Advisors may look up a subject's record information for advisory purposes. In this context,
  "record information" refers only to the subject's present status.

## Which tool operations each agent role may reach (outbound, target side)
- The advisory agent's role covers looking up a subject's current status and historical record.
