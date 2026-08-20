# Access Control Policy — ambiguous-clause evaluation scenario (reworded)

Access should be granted sparingly: a (role, scope) pair is allowed only when this document says
so, and everything else is refused.

## Which agents each user may call (inbound)
- The enrollment-advisor role can use the registrar-access capability.

## Which tool operations each user may reach (outbound, subject side)
- Enrollment advisors may look up enrollment information for advising purposes.

## Which tool operations each agent role may reach (outbound, target side)
- The registrar_operations role covers looking up enrollment status and enrollment history.
