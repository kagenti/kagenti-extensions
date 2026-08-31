# Access Control Policy — empty-descriptions evaluation scenario (reworded)

Access should be granted sparingly: a (role, scope) pair is allowed only when this document says
so, and everything else is refused.

## Which agents each user may call (inbound)
- The operator role can use a device-control agent.

## Which tool operations each user may reach (outbound, subject side)
- Operators may open and close a device.

## Which tool operations each agent role may reach (outbound, target side)
- The device-control agent's role covers opening and closing the device.
