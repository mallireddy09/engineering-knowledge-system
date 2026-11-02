from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from ekm.models import Record, migrate_record
from ekm.security import redact


def test_schema_round_trip_utc_uuid_and_hash():
    record = Record(kind="task", title="Mechanism", created_at="2026-01-01T12:00:00+03:00").sealed()
    assert record.created_at.hour == 9 and record.created_at.tzinfo == timezone.utc
    assert len(record.content_hash) == 64
    assert migrate_record(record.model_dump(mode="json")) == record
    copied = Record(kind="task", title="Mechanism").sealed()
    assert copied.content_hash == record.content_hash


@pytest.mark.parametrize(
    "data",
    [
        {"title": " "},
        {"schema_version": 2},
        {"unexpected": "data"},
        {"created_at": datetime(2026, 1, 1)},
        {"review_schedule": []},
        {"review_schedule": [7, 1]},
        {"review_schedule": [0]},
        {"kind": "question"},
        {"kind": "review"},
        {"score": 1.5},
        {"review_count": 1, "correct_count": 2},
    ],
)
def test_invalid_records_fail(data):
    with pytest.raises((ValueError, ValidationError)):
        Record.model_validate({"kind": "task", "title": "Task", **data})


def test_future_schema_not_silently_downgraded():
    with pytest.raises(ValueError, match="unsupported"):
        migrate_record({"schema_version": 9, "kind": "task", "title": "Future"})


def test_recursive_redaction_and_private_key():
    secret = "-----BEGIN PRIVATE KEY-----\nsecret\n-----END PRIVATE KEY-----"
    data = redact(
        {
            "nested": ["Bearer sensitive", "password=hunter2", secret],
            "access_token": "token",
            "safe": "explain backpressure",
        }
    )
    assert data["nested"] == ["[REDACTED]"] * 3
    assert data["access_token"] == "[REDACTED]"
    assert data["safe"] == "explain backpressure"


@pytest.mark.parametrize(
    "text",
    [
        '{"password": "EXAMPLE SECRET VALUE"}',
        'password="EXAMPLE SECRET VALUE"',
        "password='EXAMPLE SECRET VALUE'",
        '{"api_key":"EXAMPLE SECRET VALUE"}',
        'client-secret="EXAMPLE \\" SECRET VALUE"',
    ],
)
def test_quoted_secret_values_are_fully_redacted(text):
    assert "SECRET" not in redact(text)
    assert "VALUE" not in redact(text)
