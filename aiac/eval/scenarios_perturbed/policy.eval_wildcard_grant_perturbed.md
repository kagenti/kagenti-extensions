# Access Control Policy — wildcard-grant evaluation scenario (reworded)

Access should be granted sparingly: a (role, scope) pair is allowed only when this document says
so, and everything else is refused.

## Which agents each user may call (inbound)
- The manager role can use a resource agent.

## Which tool operations each user may reach (outbound, subject side)
- Managers are cleared for every resource operation there is: checking levels, adjusting counts,
  and placing orders.

## Which tool operations each agent role may reach (outbound, target side)
- The resource agent's role spans every resource operation there is: checking levels, adjusting
  counts, and placing orders.
