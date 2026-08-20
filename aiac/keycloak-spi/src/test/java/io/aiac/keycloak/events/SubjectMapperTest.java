package io.aiac.keycloak.events;

import org.junit.jupiter.api.Test;

import java.util.Optional;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

class SubjectMapperTest {

    @Test
    void clientCreatedMapsToServiceSubject() {
        Optional<String> subject =
                SubjectMapper.subjectFor(SubjectMapper.ResourceKind.CLIENT, "CREATE", "clients/abc-123");
        assertEquals(Optional.of("aiac.apply.service.abc-123"), subject);
    }

    @Test
    void clientUpdateIsDropped() {
        Optional<String> subject =
                SubjectMapper.subjectFor(SubjectMapper.ResourceKind.CLIENT, "UPDATE", "clients/abc-123");
        assertTrue(subject.isEmpty());
    }

    @Test
    void realmRoleCreatedMapsToRoleSubject() {
        Optional<String> subject =
                SubjectMapper.subjectFor(SubjectMapper.ResourceKind.REALM_ROLE, "CREATE", "roles/editor");
        assertEquals(Optional.of("aiac.apply.role.editor"), subject);
    }

    @Test
    void realmRoleUpdatedMapsToRoleSubject() {
        Optional<String> subject =
                SubjectMapper.subjectFor(SubjectMapper.ResourceKind.REALM_ROLE, "UPDATE", "roles/editor");
        assertEquals(Optional.of("aiac.apply.role.editor"), subject);
    }

    @Test
    void clientRoleCreatedMapsToRoleSubjectUsingTrailingSegment() {
        Optional<String> subject = SubjectMapper.subjectFor(SubjectMapper.ResourceKind.CLIENT_ROLE, "CREATE",
                "clients/abc-123/roles/writer");
        assertEquals(Optional.of("aiac.apply.role.writer"), subject);
    }

    @Test
    void dottedRealmRoleNameIsEncodedToASingleSubjectToken() {
        Optional<String> subject =
                SubjectMapper.subjectFor(SubjectMapper.ResourceKind.REALM_ROLE, "CREATE", "roles/team.admin");
        // "%2E" keeps "team.admin" as one NATS token so "aiac.apply.role.*" still matches it —
        // a literal "." would split it into two tokens and the consumer's filter would miss it.
        assertEquals(Optional.of("aiac.apply.role.team%2Eadmin"), subject);
    }

    @Test
    void roleNameWithReservedSubjectCharactersIsEncoded() {
        Optional<String> subject =
                SubjectMapper.subjectFor(SubjectMapper.ResourceKind.CLIENT_ROLE, "UPDATE", "clients/abc-123/roles/a*b>c d%e");
        assertEquals(Optional.of("aiac.apply.role.a%2Ab%3Ec%20d%25e"), subject);
    }

    @Test
    void roleNameWithTabCarriageReturnAndNewlineIsEncoded() {
        Optional<String> subject =
                SubjectMapper.subjectFor(SubjectMapper.ResourceKind.REALM_ROLE, "CREATE", "roles/a\tb\rc\nd");
        assertEquals(Optional.of("aiac.apply.role.a%09b%0Dc%0Ad"), subject);
    }

    @Test
    void otherResourceKindsAreDropped() {
        Optional<String> subject =
                SubjectMapper.subjectFor(SubjectMapper.ResourceKind.OTHER, "CREATE", "users/some-user");
        assertTrue(subject.isEmpty());
    }

    @Test
    void malformedResourcePathIsDroppedNotThrown() {
        Optional<String> subject =
                SubjectMapper.subjectFor(SubjectMapper.ResourceKind.CLIENT, "CREATE", "not-a-clients-path");
        assertTrue(subject.isEmpty());
    }

    @Test
    void nullResourcePathIsDroppedNotThrown() {
        Optional<String> subject = SubjectMapper.subjectFor(SubjectMapper.ResourceKind.CLIENT, "CREATE", null);
        assertTrue(subject.isEmpty());
    }

    @Test
    void payloadIsMinimalJsonWithId() {
        assertEquals("{\"id\":\"abc-123\"}", SubjectMapper.payloadFor("abc-123"));
    }

    @Test
    void payloadEscapesQuotesAndBackslashes() {
        // Without escaping, a quote or backslash in entityId would produce malformed or
        // injected JSON (e.g. a crafted id could inject extra fields into the payload).
        assertEquals("{\"id\":\"a\\\"b\\\\c\"}", SubjectMapper.payloadFor("a\"b\\c"));
    }

    @Test
    void payloadEscapesControlCharacters() {
        assertEquals("{\"id\":\"a\\nb\\u0001c\"}", SubjectMapper.payloadFor("a\nb\u0001c"));
    }
}
