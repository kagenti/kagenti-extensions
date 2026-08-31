# Access Control Policy — confusable-agents evaluation scenario

Grant access on a least-privilege basis. Only grant a (role, scope) pair when this
policy supports it; deny by default.

## Users → agent capabilities (inbound; user may call an agent)
- Trainers may use one agent's coordination capabilities.
- Analysts may use a different agent's review capabilities.

## Users → tool operations (outbound subject; user may reach a tool operation)
- Trainers may read a roster and write to a schedule.
- Analysts may read and write evaluation records.

## Agent roles → tool operations (outbound target; an agent role may reach a tool
## operation)
- The coordination agent's role may read the roster and write the schedule.
- The review agent's role may read and write evaluation records.
