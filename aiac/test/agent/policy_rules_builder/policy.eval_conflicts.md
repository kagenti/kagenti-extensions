# Access Control Policy

Grant access on a least-privilege basis; deny by default.

## Users -> tool operations (subject may reach the tool)
- release-user may perform deploy-trigger and deploy-status.
- qa-user may perform deploy-status.

## Restrictions on deployment operations
- release-user must never be granted deploy-trigger under any circumstance; this permission is
  permanently revoked pending the outcome of the ongoing security review and must not be
  reinstated by any other clause in this document.
