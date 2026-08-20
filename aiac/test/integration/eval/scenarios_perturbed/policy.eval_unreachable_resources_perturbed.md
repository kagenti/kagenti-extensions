# Access Control Policy — unreachable-resources evaluation scenario (reworded)

Access should be granted sparingly: a (role, scope) pair is allowed only when this document says
so, and everything else is refused.

## Which agents each user may call (inbound)
- The front-desk-clerk role can use the intake-access capability.

## Which tool operations each user may reach (outbound, subject side)
- Front desk clerks are permitted to read and write patient records.

## Which tool operations each agent role may reach (outbound, target side)
- The intake_operations role covers reading and writing patient records.
