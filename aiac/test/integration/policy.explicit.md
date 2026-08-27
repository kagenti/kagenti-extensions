# Access Control Policy — github-agent / github-tool

Grant access on a least-privilege basis. Only grant a (role, scope) pair when this
policy supports it; deny by default.

## Users → tool operations (outbound subject; user may reach the tool)
- developer may perform source-read, source-write, and issues-read.
- tester may perform issues-read and issues-write.

## Agent roles → tool operations (outbound target; agent may reach the tool)
- source_operations may perform source-read and source-write.
- issue_operations may perform issues-read and issues-write.
