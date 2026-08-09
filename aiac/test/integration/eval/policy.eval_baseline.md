# Access Control Policy — baseline-scale evaluation scenario

Grant access on a least-privilege basis. Only grant a (role, scope) pair when this
policy supports it; deny by default.

## Users → agent capabilities (inbound; user may call an agent)
- code-editor may use code-access.
- deploy-manager may use orchestration-access.
- issue-triager may use tracker-access.
- read-only-observer may use code-access and tracker-access.
- security-reviewer may use orchestration-access.

## Users → tool operations (outbound subject; user may reach a tool operation, or a
## capability delegated by one agent to another, through the agent it calls)
- code-editor may perform quill-read and quill-write.
- deploy-manager may perform beacon-deploy, beacon-status, and code-delegation.
- issue-triager may perform ledger-read and ledger-write.
- read-only-observer may perform quill-read and ledger-read.
- security-reviewer may perform vault-read.

## Agent roles → tool operations (outbound target; an agent role may reach a tool
## operation, or a capability delegated to it by another agent)
- code_operations may perform quill-read and quill-write.
- tracker_operations may perform ledger-read and ledger-write.
- orchestration_operations may perform beacon-deploy, beacon-status, vault-read, and
  code-delegation.
