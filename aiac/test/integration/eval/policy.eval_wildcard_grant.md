# Access Control Policy — wildcard-grant evaluation scenario

Grant access on a least-privilege basis. Only grant a (role, scope) pair when this
policy supports it; deny by default.

## Users → agent capabilities (inbound; user may call an agent)
- inventory-manager may use inventory-access.

## Users → tool operations (outbound subject; user may reach a tool operation)
- inventory-manager is authorized to perform all inventory operations: checking stock
  levels, adjusting counts, and placing reorders.

## Agent roles → tool operations (outbound target; an agent role may reach a tool
## operation)
- inventory_operations covers all inventory operations against the inventory tool:
  checking stock levels, adjusting counts, and placing reorders.
