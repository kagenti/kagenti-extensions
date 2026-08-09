# Access Control Policy — baseline evaluation scenario

Grant access on a least-privilege basis. Only grant a (role, scope) pair when this
policy supports it; deny by default.

## Users → agent capabilities (inbound; user may call an agent)
- developer may use repo-access and tracker-access.
- tester may use tracker-access.

## Users → tool operations (outbound subject; user may reach a tool operation)
- developer may perform repo-read, repo-write, and tracker-read.
- tester may perform tracker-read and tracker-write.

## Agent roles → tool operations (outbound target; an agent role may reach a tool
## operation)
- repo_operations may perform repo-read and repo-write.
- tracker_operations may perform tracker-read and tracker-write.
