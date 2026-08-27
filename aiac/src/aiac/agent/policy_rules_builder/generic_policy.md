# Generic Access Control Policy

This baseline policy applies to every policy decision, on top of the
scenario-specific policy that follows it. Read both together as one policy.

- The agent's internal operator roles are each confined to their own domain: grant every operator role the target operations — where a target is a tool the agent calls, or another agent it calls — within the domain it is responsible for. (This baseline only grants; a pair outside an operator role's domain is simply left ungranted — a silent non-grant — never an explicit prohibition.)
