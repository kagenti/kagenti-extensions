package io.aiac.keycloak.events;

import io.nats.client.Connection;
import org.jboss.logging.Logger;
import org.keycloak.events.Event;
import org.keycloak.events.EventListenerProvider;
import org.keycloak.events.admin.AdminEvent;
import org.keycloak.events.admin.ResourceType;

import java.nio.charset.StandardCharsets;

/**
 * Thin publisher: on a matching admin event, publishes a minimal {@code {"id": "..."}} payload
 * to the corresponding AIAC subject. {@code CLIENT_CREATED} and role create/update are admin
 * events in modern Keycloak (resourceType/operationType), not the legacy user-facing
 * {@code EventType} enum — so {@link #onEvent(Event)} is a no-op by design (drops
 * REGISTER/UPDATE_PROFILE/etc.; OPA rules are role-scoped and resolve entitlements from the
 * caller's role automatically).
 */
public class AiacEventListenerProvider implements EventListenerProvider {

    private static final Logger log = Logger.getLogger(AiacEventListenerProvider.class);

    private final Connection natsConnection;

    public AiacEventListenerProvider(Connection natsConnection) {
        this.natsConnection = natsConnection;
    }

    @Override
    public void onEvent(Event event) {
        // No-op — see class javadoc.
    }

    @Override
    public void onEvent(AdminEvent event, boolean includeRepresentation) {
        SubjectMapper.ResourceKind kind = toResourceKind(event.getResourceType());
        String operationType = event.getOperationType() == null ? null : event.getOperationType().name();

        SubjectMapper.subjectFor(kind, operationType, event.getResourcePath()).ifPresent(this::publish);
    }

    @Override
    public void close() {
        // No-op: natsConnection is shared across every provider instance this request's
        // factory creates, and is owned/closed by the factory (postInit/close), not here.
    }

    private void publish(String subject) {
        if (natsConnection == null) {
            log.warnf("NATS connection unavailable; dropping event for subject %s", subject);
            return;
        }
        // Subjects are always "aiac.apply.<type>.<id>" and ids never contain '.' (UUID or role
        // name), so the trailing segment after the last '.' is exactly the id SubjectMapper built
        // the subject from.
        String entityId = subject.substring(subject.lastIndexOf('.') + 1);
        String payload = SubjectMapper.payloadFor(entityId);
        natsConnection.publish(subject, payload.getBytes(StandardCharsets.UTF_8));
    }

    private static SubjectMapper.ResourceKind toResourceKind(ResourceType resourceType) {
        if (resourceType == null) {
            return SubjectMapper.ResourceKind.OTHER;
        }
        switch (resourceType) {
            case CLIENT:
                return SubjectMapper.ResourceKind.CLIENT;
            case REALM_ROLE:
                return SubjectMapper.ResourceKind.REALM_ROLE;
            case CLIENT_ROLE:
                return SubjectMapper.ResourceKind.CLIENT_ROLE;
            default:
                return SubjectMapper.ResourceKind.OTHER;
        }
    }
}
