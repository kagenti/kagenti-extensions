# Access Control Policy — baseline evaluation scenario (reworded)

Access should be granted sparingly: a (role, scope) pair is allowed only when this document says
so, and everything else is refused.

## Which agents each user may call (inbound)
- The developer role can use both the source repository agent and the issue tracker agent.
- The tester role can use the issue tracker agent.

## Which tool operations each user may reach (outbound, subject side)
- Developers are permitted to read and write the repository, and to read the tracker.
- Testers are permitted to read and write the tracker.

## Which tool operations each agent role may reach (outbound, target side)
- The source repository agent's role covers reading and writing the repository.
- The issue tracker agent's role covers reading and writing the tracker.
