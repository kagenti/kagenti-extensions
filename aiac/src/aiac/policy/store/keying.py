"""Slash-safe encoding of a Policy Store ``service_id`` for use as a URL path segment.

A service_id is the Keycloak clientId, which contains slashes (``ns/workload``, or a SPIFFE URI
under SPIRE) and cannot be a single path segment. The library encodes it with URL-safe base64
(no ``/``, ``:``, or ``=`` padding) before putting it in the path; the service decodes it back.
The decoded id stays the cache/DB key and the ``service_id`` in every SPM body.
"""
import base64
import re

# URL-safe base64 (minus ``=`` padding) yields only these characters. Asserting it before the
# value is spliced into a request URL proves the encoded id is a single, inert path segment —
# no scheme, host, ``/``, ``.`` or ``..`` can be injected (closes the partial-SSRF vector).
_SEGMENT_RE = re.compile(r"[A-Za-z0-9_-]+")


def encode_service_id(service_id: str) -> str:
    encoded = base64.urlsafe_b64encode(service_id.encode("utf-8")).decode("ascii").rstrip("=")
    if not _SEGMENT_RE.fullmatch(encoded):  # unreachable given the alphabet above; defends the invariant
        raise ValueError("encoded service_id is not a safe URL path segment")
    return encoded


def decode_service_id(encoded: str) -> str:
    padding = "=" * (-len(encoded) % 4)
    return base64.urlsafe_b64decode(encoded + padding).decode("utf-8")
