package io.aiac.keycloak.events;

import java.util.Map;
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
                    return lastSegment(resourcePath).map(name -> "aiac.apply.role." + encodeSubjectToken(name));
                }
                return Optional.empty();
            default:
                return Optional.empty();
        }
    }

    /** Minimal JSON payload — the event is a trigger, not a data carrier (see event-broker.md). */
    public static String payloadFor(String entityId) {
        return "{\"id\":\"" + escapeJson(entityId) + "\"}";
    }

    /**
     * Minimal JSON string escaping (RFC 8259) for the one field this class ever serializes.
     * Hand-rolled rather than pulling in a JSON library, to keep this class dependency-free
     * (see the class javadoc) — {@code entityId} is normally a Keycloak UUID or resource name,
     * but nothing stops it containing a quote, backslash, or control character.
     */
    private static String escapeJson(String raw) {
        StringBuilder out = new StringBuilder(raw.length());
        for (int i = 0; i < raw.length(); i++) {
            char c = raw.charAt(i);
            switch (c) {
                case '"':
                    out.append("\\\"");
                    break;
                case '\\':
                    out.append("\\\\");
                    break;
                case '\n':
                    out.append("\\n");
                    break;
                case '\r':
                    out.append("\\r");
                    break;
                case '\t':
                    out.append("\\t");
                    break;
                case '\b':
                    out.append("\\b");
                    break;
                case '\f':
                    out.append("\\f");
                    break;
                default:
                    if (c < 0x20) {
                        out.append(String.format("\\u%04x", (int) c));
                    } else {
                        out.append(c);
                    }
            }
        }
        return out.toString();
    }

    /**
     * Characters significant to NATS subject tokenization: {@code .} separates tokens, {@code *}
     * and {@code >} are wildcards, and whitespace is disallowed in a token. Role names (unlike the
     * client UUIDs used for {@code aiac.apply.service.*}) are free-form and may contain any of
     * these, so they are percent-encoded here into a single token. {@code %} itself is escaped too
     * so the mapping stays reversible. The Agent consumer decodes this with the mirror-image
     * operation (Python's {@code urllib.parse.unquote}) before using the role name.
     */
    private static final Map<Character, String> SUBJECT_TOKEN_ESCAPES =
            Map.of(
                    '%', "%25",
                    '.', "%2E",
                    '*', "%2A",
                    '>', "%3E",
                    ' ', "%20",
                    '\t', "%09",
                    '\r', "%0D",
                    '\n', "%0A");

    private static String encodeSubjectToken(String raw) {
        StringBuilder out = new StringBuilder();
        for (int i = 0; i < raw.length(); i++) {
            char c = raw.charAt(i);
            out.append(SUBJECT_TOKEN_ESCAPES.getOrDefault(c, String.valueOf(c)));
        }
        return out.toString();
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
