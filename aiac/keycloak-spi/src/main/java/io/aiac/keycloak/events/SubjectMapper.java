package io.aiac.keycloak.events;

import java.util.Optional;

/**
 * Maps a Keycloak admin event (resource kind + operation type + resource path) to the AIAC NATS
 * subject and payload it should be published as. Deliberately free of any Keycloak imports so it
 * is unit-testable in isolation, without a running Keycloak server.
 */
public final class SubjectMapper {

    /** Local mirror of the {@code org.keycloak.events.admin.ResourceType} values this SPI cares about. */
    public enum ResourceKind {
        CLIENT,
        REALM_ROLE,
        CLIENT_ROLE,
        OTHER,
    }

    private SubjectMapper() {
    }

    /**
     * @param kind          the admin event's resource kind
     * @param operationType {@code org.keycloak.events.admin.OperationType} name, e.g. "CREATE"
     * @param resourcePath  e.g. {@code "clients/{uuid}"}, {@code "roles/{name}"}, or
     *                      {@code "clients/{uuid}/roles/{name}"}
     * @return the AIAC subject to publish on, or empty if this event should be dropped
     */
    public static Optional<String> subjectFor(ResourceKind kind, String operationType, String resourcePath) {
        if (resourcePath == null || kind == null) {
            return Optional.empty();
        }
        switch (kind) {
            case CLIENT:
                if ("CREATE".equals(operationType)) {
                    return lastSegmentAfter(resourcePath, "clients/").map(id -> "aiac.apply.service." + id);
                }
                return Optional.empty();
            case REALM_ROLE:
            case CLIENT_ROLE:
                if ("CREATE".equals(operationType) || "UPDATE".equals(operationType)) {
                    return lastSegment(resourcePath).map(name -> "aiac.apply.role." + name);
                }
                return Optional.empty();
            default:
                return Optional.empty();
        }
    }

    /** Minimal JSON payload — the event is a trigger, not a data carrier (see event-broker.md). */
    public static String payloadFor(String entityId) {
        return "{\"id\":\"" + entityId + "\"}";
    }

    private static Optional<String> lastSegmentAfter(String path, String prefix) {
        int idx = path.indexOf(prefix);
        if (idx < 0) {
            return Optional.empty();
        }
        String rest = path.substring(idx + prefix.length());
        return rest.isEmpty() ? Optional.empty() : Optional.of(rest.split("/")[0]);
    }

    private static Optional<String> lastSegment(String path) {
        String[] parts = path.split("/");
        String last = parts[parts.length - 1];
        return last.isEmpty() ? Optional.empty() : Optional.of(last);
    }
}
