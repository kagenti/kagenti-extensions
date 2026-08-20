# Access Control Policy — wildcard-grant evaluation scenario (reworded)

Access should be granted sparingly: a (role, scope) pair is allowed only when this document says
so, and everything else is refused.

## Which agents each user may call (inbound)
- The inventory-manager role can use the inventory-access capability.

## Which tool operations each user may reach (outbound, subject side)
- Inventory managers are cleared for every inventory operation there is: checking stock, adjusting
  counts, and placing reorders.

## Which tool operations each agent role may reach (outbound, target side)
- The inventory_operations role spans every inventory operation against the inventory tool:
  checking stock, adjusting counts, and placing reorders.
