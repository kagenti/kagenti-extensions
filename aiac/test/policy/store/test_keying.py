"""Unit tests for aiac/policy/store/keying.py — slash-safe service_id encoding."""

import pytest

from aiac.policy.store.keying import decode_service_id, encode_service_id


@pytest.mark.parametrize(
    "service_id",
    [
        "github-agent",
        "team1/github-agent",
        "spiffe://localtest.me/ns/team1/sa/github-agent",
    ],
)
def test_round_trips(service_id):
    encoded = encode_service_id(service_id)
    assert decode_service_id(encoded) == service_id


@pytest.mark.parametrize(
    "service_id",
    [
        "github-agent",
        "team1/github-agent",
        "spiffe://localtest.me/ns/team1/sa/github-agent",
    ],
)
def test_encoding_is_path_segment_safe(service_id):
    encoded = encode_service_id(service_id)
    assert "/" not in encoded
    assert ":" not in encoded
    assert "=" not in encoded
