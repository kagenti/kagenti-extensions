# AIAC Keycloak Event Listener

A Keycloak Event Listener SPI that translates Keycloak **admin events** into NATS publishes on
the AIAC Event Broker (see [`../docs/specs/components/event-broker.md`](../docs/specs/components/event-broker.md)
and PRD §7.10). It is a pure publisher — no business logic, same "thin adapter" philosophy as
the AIAC Agent's own NATS consumer (`../src/aiac/agent/eventbus/`).

This module only builds and packages the SPI. It does **not** wire itself into any running
Keycloak deployment — the real Keycloak instance for this platform is deployed by a separate
Helm chart outside this repo. Installing/enabling the listener there is a manual step, documented
below.

## Event → subject mapping

| Keycloak admin event | AIAC subject | Notes |
|---|---|---|
| `CLIENT` + `CREATE` (`CLIENT_CREATED`) | `aiac.apply.service.{internal-uuid}` | id parsed from `resourcePath` (`clients/{uuid}`) — this is the Keycloak *internal* client UUID, which is what `onboard_service()` expects, not the human-readable `clientId`. |
| `REALM_ROLE` / `CLIENT_ROLE` + `CREATE` or `UPDATE` | `aiac.apply.role.{role-name}` | id is the trailing path segment (a role **name**, not a UUID). `update_role()` is currently a stub with no finalized ID contract (UC3 not yet implemented) — revisit this mapping once UC3 lands. |
| everything else (user events, `DELETE`, etc.) | — | dropped; OPA rules are role-scoped and resolve entitlements from the caller's role automatically. |

Payload is always the minimal `{"id": "<entity-id>"}` — the event is a trigger, not a data
carrier; the AIAC Agent pulls all state it needs from the IdP Configuration Service.

`onEvent(Event)` (the legacy user-facing event stream) is a no-op by design — `CLIENT_CREATED`
and role create/update are **admin** events in modern Keycloak, delivered via
`onEvent(AdminEvent, boolean)`.

## Build

```sh
mvn package          # -> target/aiac-event-listener-0.1.0.jar (shaded — bundles jnats)
mvn test             # SubjectMapperTest only; see Testing below
```

Or via the Makefile: `make package` / `make test`.

## Build the custom Keycloak image

```sh
REGISTRY=ghcr.io/your-org make image   # builds + loads locally
REGISTRY=ghcr.io/your-org make push    # builds + pushes
```

The Dockerfile is a 3-stage build: compile the shaded jar, drop it into
`/opt/keycloak/providers/` and run `kc.sh build` (bakes the augmented server into the image, no
per-pod build at startup), then copy the built distribution into the final runtime image.

## Install

**Jar-only** (existing Keycloak/RHBK deployment):

1. Copy `target/aiac-event-listener-0.1.0.jar` into `/opt/keycloak/providers/`.
2. Run `kc.sh build`.
3. Restart Keycloak.

**Custom image** (this repo does not automate this step — the Keycloak deployment lives in a
separate Helm chart outside this repo):

1. `make push` to publish the image.
2. Override the Keycloak image reference in that chart's values (or Operator CR) to point at
   the pushed image.

## Enable in a realm

The listener is discovered automatically via Java's `ServiceLoader` once the jar is on the
classpath, but it must still be added to the realm's admin-events listener list — either in the
Admin Console (**Realm Settings → Events → Event Listeners**, add `aiac-event-listener`) or via
the Admin REST API:

```sh
curl -X PUT "$KEYCLOAK_URL/admin/realms/$REALM/events/config" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"adminEventsEnabled": true, "eventsListeners": ["jboss-logging", "aiac-event-listener"]}'
```

(`eventsListeners` replaces the whole list — include Keycloak's default `jboss-logging` listener
unless you intend to remove it.)

## Configure `NATS_URL`

Resolved in this order, in `AiacEventListenerProviderFactory.init()`:

1. SPI config value `natsUrl` — settable via the Keycloak SPI env var convention:
   `KC_SPI_EVENTS_LISTENER_AIAC_EVENT_LISTENER_NATS_URL=nats://aiac-event-broker-service:4222`
   (pattern: `KC_SPI_<spi-id>_<provider-id>_<property>`, uppercased, dashes→underscores; the SPI
   id for event listeners is `events-listener`).
2. Plain `NATS_URL` environment variable.
3. Default: `nats://aiac-event-broker-service:4222`.

Setting either on the live Keycloak pod is the separate Helm chart's responsibility, not this
repo's — this code is ready for it either way. Verify the exact SPI env var naming against a
running Keycloak 26.6.3 instance before relying on it in a deploy runbook; it's inferred from
Keycloak's documented CLI-flag-to-env-var convention, not confirmed here.

## Testing

`SubjectMapperTest` unit-tests the id-parsing/subject-building logic in isolation with plain
JUnit — no Keycloak server, no mocks of `KeycloakSession`/`AdminEvent`. The
`EventListenerProvider`/`EventListenerProviderFactory` classes themselves are thin wrappers
around that logic and are not unit tested; Keycloak SPI integration testing without a running
server is impractical. To verify manually:

1. Build and run the custom image locally (or install the jar into a dev Keycloak).
2. Enable the listener in a realm (see above) and set `NATS_URL` to a reachable broker.
3. `nats sub 'aiac.apply.>'` against that broker.
4. Create a client (or a role) via the Admin Console / REST API and confirm a message appears
   on the expected subject with the expected id.

## Known gaps / open questions

- **Role ID format is unverified.** `update_role()` in the AIAC Agent is currently a stub with
  no real ID contract — this listener's choice of "role name, not UUID" may need revisiting once
  UC3 (Role Update) is actually implemented.
- **jnats and maven-shade-plugin versions** are pinned to the latest known-stable values as of
  this module's creation — re-check Maven Central before relying on them long-term.
- **SPI env var naming** for the event-listener SPI id (`events-listener`) is inferred from
  Keycloak's documented convention, not confirmed against a running instance.
