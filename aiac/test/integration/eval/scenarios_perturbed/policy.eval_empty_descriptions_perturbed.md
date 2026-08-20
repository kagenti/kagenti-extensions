# Access Control Policy — empty-descriptions evaluation scenario (reworded)

Access should be granted sparingly: a (role, scope) pair is allowed only when this document says
so, and everything else is refused.

## Which agents each user may call (inbound)
- The field-operator role can use the irrigation-access capability.

## Which tool operations each user may reach (outbound, subject side)
- Field operators may open and close valves.

## Which tool operations each agent role may reach (outbound, target side)
- The irrigation_operations role covers opening and closing valves.
