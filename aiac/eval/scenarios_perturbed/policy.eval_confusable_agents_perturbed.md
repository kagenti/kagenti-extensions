# Access Control Policy — confusable-agents evaluation scenario (reworded)

Access should be granted sparingly: a (role, scope) pair is allowed only when this document says
so, and everything else is refused.

## Which agents each user may call (inbound)
- The trainer role can use one agent's coordination capability.
- The analyst role can use a different agent's review capability.

## Which tool operations each user may reach (outbound, subject side)
- Trainers may look up the roster and update the schedule.
- Analysts may look up and record evaluations.

## Which tool operations each agent role may reach (outbound, target side)
- The coordination agent's role covers looking up the roster and updating the schedule.
- The review agent's role covers looking up and recording evaluations.
