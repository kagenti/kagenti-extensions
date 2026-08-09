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
}
