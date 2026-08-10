# Component PRD: Keycloak SPI Listener

## Description

A custom Keycloak **Event Listener SPI** (Java) that listens to Keycloak's internal admin-event bus and translates entity lifecycle events into NATS publish calls on the [Event Broker](event-broker.md). It is a pure publisher — no business logic, the same "thin adapter" philosophy as the AIAC Agent's own NATS consumer (`aiac/agent/eventbus/`).

The SPI is the trigger source for two of the Agent's three automated use cases: new service onboarding (`CLIENT_CREATED`) and role change (role create/update). It carries no policy knowledge and no state — it emits a minimal `{ "id": "<entity-id>" }` trigger and lets the Agent pull all required state from the IdP Configuration Service at processing time.

Source lives at `aiac/keycloak-spi/`. It is a Java/Maven module, not part of the Python `aiac` package, and is built and released independently (see [Build & Deploy](#build--deploy)).

This PRD documents the **as-built** behavior of the implemented SPI and resolves the open questions previously tracked in `keycloak-spi/README.md`.

---

## Event → Subject Mapping

The SPI subscribes to Keycloak **admin events** (`onEvent(AdminEvent, boolean)`). The legacy user-facing event stream (`onEvent(Event)`) is a **no-op by design** — `CLIENT_CREATED` and role create/update are admin events in modern Keycloak (`ResourceType` + `OperationType`), not entries in the user-facing `EventType` enum. User events (`REGISTER`, `UPDATE_PROFILE`, …) are dropped, because OPA rules are role-scoped and resolve entitlements from the caller's role automatically.

| Keycloak admin event (`ResourceType` + `OperationType`) | Event Broker subject | Entity ID source |
|---|---|---|
| `CLIENT` + `CREATE` | `aiac.apply.service.{internal-uuid}` | segment after `clients/` in `resourcePath` (`clients/{uuid}`) — the Keycloak **internal client UUID**, not the human-readable `clientId` |
| `REALM_ROLE` + `CREATE` \| `UPDATE` | `aiac.apply.role.{role-name}` | trailing segment of `resourcePath` (`roles/{name}`) — the role **name** |
| `CLIENT_ROLE` + `CREATE` \| `UPDATE` | `aiac.apply.role.{role-name}` | trailing segment of `resourcePath` (`clients/{uuid}/roles/{name}`) — the role **name** |
| everything else (other resource kinds, `DELETE`, user events) | — (dropped) | — |

`CLIENT_ROLE` is a deliberate, harmless **superset** of the AIAC PRD's realm-role-only mention: a client role and a realm role both map to `aiac.apply.role.{name}` using the trailing path segment, so no additional handling is needed on the Agent side.

The subject mapping is isolated in a pure `SubjectMapper` class with **no Keycloak imports**, so it is unit-testable without a running server (see [Testing](#testing)). Malformed or `null` resource paths are dropped, never thrown.

The AIAC Agent subject schema (documented in [event-broker.md](event-broker.md) and PRD §7.6) is **authoritative**; this SPI is one publisher against it. The `aiac.apply.service.{id}` / `aiac.apply.role.{id}` subjects here are the same ones the Agent's consumer dispatches on (`onboard_service` / `update_role`).

---

## Message Payload

Every publish carries the minimal JSON payload:

```json
{ "id": "<entity-id>" }
```

The event is a **trigger, not a data carrier** — consistent with [event-broker.md](event-broker.md). The entity ID is the trailing segment of the subject (subjects are always `aiac.apply.<type>.<id>` and IDs never contain `.`), so the payload ID always equals the ID encoded in the subject. The Agent uses it to pull full entity state from the IdP Configuration Service.

---

## Publish Semantics & Retry Policy

- **Core NATS publish.** The SPI publishes to the subject with a plain NATS publish. The Event Broker's `aiac-events` stream (subjects `aiac.apply.>`, `WorkQueuePolicy`) captures the message; **durability, replay, and DLQ handling all live on the consumer/broker side** (see [event-broker.md](event-broker.md)), not in the SPI.
- **No application-level retry or buffering.** The SPI does not retry individual publishes and does not queue events locally. End-to-end at-least-once delivery begins once a message lands in the JetStream stream.
- **Transport-level reconnect.** The shared NATS connection uses jnats' built-in auto-reconnect, so a broker that drops after a connection is established is transparently reconnected.
- **Fail-open at startup.** The NATS connection is opened once by the provider **factory** (`postInit`) and shared across every per-request provider instance; it is closed by the factory (`close`). If the broker is unreachable at Keycloak startup, the connection is left null, Keycloak startup is **never failed**, and matching events are dropped with a `WARN` log until the deployment is fixed. This is a deliberate availability tradeoff: the SPI must never take Keycloak down.

The publish boundary is therefore **best-effort (at-most-once)**; the platform's durability guarantee starts at the stream. Missed triggers (broker down at emit time) are recovered by the operator-only `rebuild` command, which reconciles full state independently of the event stream.

---

## Configuration

| Variable | Default | Source |
|---|---|---|
| `NATS_URL` | `nats://aiac-event-broker-service:4222` | ConfigMap (`aiac-pdp-config`) — the same ConfigMap used by the Agent and RAG Ingest Service |

`NATS_URL` is resolved in `AiacEventListenerProviderFactory.init()` in this order:

1. Keycloak SPI config value `natsUrl` (see the override key below);
2. the plain `NATS_URL` environment variable;
3. the cluster default `nats://aiac-event-broker-service:4222`.

**The deployment uses the plain `NATS_URL` env var** injected from `aiac-pdp-config` — the deterministic, ambiguity-free path, matching every other AIAC component. The Keycloak SPI-scoped override key is available if ever needed (see open question 3 below).

No authentication credentials are required. Consistent with PRD §10, the Event Broker has **no auth** — ClusterIP network isolation is the access control mechanism.

---

## Build & Deploy

- **Language / build:** Java 17, Maven. Artifact: `target/aiac-event-listener-0.1.0.jar`, **shaded** so the compile-scope `jnats` dependency (not on Keycloak's classpath) is bundled onto the ServiceLoader runtime classpath. All `keycloak-*` dependencies are `provided`.
- **Provider discovery:** registered via `META-INF/services/org.keycloak.events.EventListenerProviderFactory`; provider id `aiac-event-listener`.
- **Custom image:** 3-stage Dockerfile — (1) build the shaded JAR, (2) drop it into `/opt/keycloak/providers/` and run `kc.sh build` so the augmented server is baked into the image (no per-pod build at startup), (3) copy the built distribution into the final runtime image. Default base `quay.io/keycloak/keycloak:26.5.2`; image ref `ghcr.io/rossoctl/cortex/aiac-keycloak-event-listener:0.1.0-kc26.5.2` (`make image` / `make push`).
- **JAR-only install:** copy the JAR into an existing Keycloak's `providers/`, run `kc.sh build`, restart.
- **CI:** like the other AIAC components (PRD §10), this SPI is **not** registered in the repo's `build.yaml` matrix; it has its own Makefile-driven build.
- **Not self-wiring:** this module builds and packages the SPI only. Deploying the image into a running Keycloak (Helm values / Operator CR image override) and enabling the listener are **manual steps owned by the separate Keycloak deployment**, not this repo.

### Enabling in a realm

The listener is discovered automatically once on the classpath, but must still be added to the realm's admin-events listener list — via **Realm Settings → Events → Event Listeners** (add `aiac-event-listener`) or the Admin REST API:

```sh
curl -X PUT "$KEYCLOAK_URL/admin/realms/$REALM/events/config" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"adminEventsEnabled": true, "eventsListeners": ["jboss-logging", "aiac-event-listener"]}'
```

(`eventsListeners` replaces the whole list — keep `jboss-logging` unless you intend to remove it.)

---

## Open Questions (Resolved)

These were tracked as "Known gaps / open questions" in `keycloak-spi/README.md`. Resolutions:

1. **Role-ID format — resolved: role *name*, provisional pending UC3.**
   The **service** path is fully confirmed: the Agent's `onboard_service` expects the Keycloak **internal client UUID** (`Service.id == Trigger.entity_id`, *not* `clientId`), which is exactly the segment the SPI extracts from `clients/{uuid}`. The **role** path targets `update_role`, which is currently a UC3 stub performing a role-keyed replace (`override=True`). Keying the subject by role **name** is consistent with that role-keyed replace semantics and is the accepted contract for now. This must be re-verified when UC3 (issue 3.11) finalizes its ID contract; if UC3 requires a UUID, the `REALM_ROLE` / `CLIENT_ROLE` extraction in `SubjectMapper` is the single point to change.

2. **jnats / shade plugin versions — resolved: pin jnats 2.x, shade 3.6.0.**
   As-built pins `jnats 2.20.6` and `maven-shade-plugin 3.6.0` against `keycloak 26.5.2` (compiler-plugin 3.13.0, surefire 3.5.2, JUnit 5.11.3). `maven-shade-plugin 3.6.0` is current. `jnats` current stable is `2.25.1` (Jan 2026); bumping is low-risk and recommended at the next maintenance pass, but 2.20.6 is a valid, tested pin. The pin policy: track jnats 2.x, re-check Maven Central at each Keycloak version bump.

3. **SPI env-var naming — resolved: `NATS_URL` is authoritative; SPI key confirmed as an override.**
   The deployment injects the plain **`NATS_URL`** env var (from `aiac-pdp-config`), which is deterministic and sidesteps camelCase normalization. Keycloak's provider-config namespace uses a **hyphenated** SPI id `events-listener`, so the CLI/override forms are:
   - CLI: `--spi-events-listener-aiac-event-listener-nats-url=<url>`
   - Env: `KC_SPI_EVENTS_LISTENER_AIAC_EVENT_LISTENER_NATS_URL=<url>`

   (pattern: `KC_SPI_<spi-id>_<provider-id>_<property>`, uppercased, dashes → underscores). These are available as overrides, but plain `NATS_URL` is the path the platform uses.

---

## Testing

Good tests here assert **external behavior** — the subject and payload produced for a given Keycloak event — not internal wiring. The `EventListenerProvider` / `EventListenerProviderFactory` classes are thin wrappers around the pure mapping logic and around jnats; unit-testing them without a running Keycloak server is impractical, so they are covered by manual/integration verification instead.

| Target | What to test | Prior art |
|---|---|---|
| `SubjectMapper` (existing seam) | Every event → subject/payload rule in isolation, plus drop-not-throw for malformed / null / unhandled events. Pure JUnit, no Keycloak server, no mocked `KeycloakSession`/`AdminEvent`. | `SubjectMapperTest` (implemented — client-created, client-update-dropped, realm/client role create+update, other-kind-dropped, malformed/null paths, payload shape) |
| Provider / Factory | Not unit-tested. Verified manually: build the image, enable the listener in a realm, `nats sub 'aiac.apply.>'`, then create a client/role and confirm a message on the expected subject with the expected id. | Event Broker consumer integration tests (require a live NATS JetStream instance) — see [event-broker.md](event-broker.md) |

The `SubjectMapper`-as-seam decision mirrors the wider AIAC testing convention: push logic into a pure, import-free unit and test it there; treat the Keycloak/NATS boundary as integration-only.

---

## Cross-References

- [event-broker.md](event-broker.md) — the NATS JetStream broker this SPI publishes to; authoritative subject schema, payload contract, delivery guarantees, and DLQ.
- PRD §7.11 (Keycloak SPI Listener) — component summary; links here as its full spec.
- `aiac/keycloak-spi/README.md` — build/deploy/config runbook.
