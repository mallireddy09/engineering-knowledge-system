"""Behavioral tests for isolation, approvals and interrupted-save recovery."""

from datetime import timedelta
from uuid import uuid4

import pytest

from ekm.config import Config, Profile
from ekm.core import Engine
from ekm.models import Kind, utcnow
from ekm.providers.base import ConflictError, StorageError
from ekm.providers.mock import MockStorageProvider


@pytest.fixture
def engine(tmp_path):
    config = Config(tmp_path / "home")
    profile = Profile(name="demo", account_type="mock")
    config.add(profile)
    config.switch(profile.name, approved=True)
    provider = MockStorageProvider(config.profile_dir(profile) / "remote")
    provider.initialize_storage(approved=True)
    result = Engine(config, profile, provider)
    yield result
    result.close()


def task(engine, **overrides):
    return engine.prepare(
        {
            "kind": "task",
            "title": "Partition consumer",
            "task_id": "APP-1",
            "project_id": "streaming",
            "technologies": ["Kafka"],
            **overrides,
        }
    )


def test_save_approval_and_dry_preview(engine):
    record = task(engine)
    preview = engine.preview(record)
    assert preview["profile"]["name"] == "demo"
    assert engine.records() == []
    with pytest.raises(ValueError, match="approval"):
        engine.save(record)
    with pytest.raises(ValueError, match="hash"):
        engine.save(record, approved=True, expected_hash="stale")
    assert engine.pending() == []
    engine.save(record, approved=True, expected_hash=preview["approval_hash"])
    assert len(engine.records()) == 1


def test_stable_task_dedup_and_explicit_update(engine):
    first = engine.save(task(engine), approved=True)
    same = engine.save(task(engine), approved=True)
    assert first.record.id == same.record.id
    assert len(engine.provider.list_records()) == 1
    revised = task(engine, implementation_summary="Backpressure control")
    with pytest.raises(ConflictError):
        engine.save(revised, approved=True)
    engine.discard(revised.id, approved=True)
    engine.save(revised, etag=first.etag, approved=True)
    assert engine.get(Kind.task, revised.id).record.implementation_summary == "Backpressure control"


def test_optimistic_concurrency_preserves_pending_draft(engine):
    first = engine.save(task(engine), approved=True)
    changed = task(engine, implementation_summary="First revision")
    engine.save(changed, etag=first.etag, approved=True)
    stale = task(engine, implementation_summary="Stale revision")
    with pytest.raises(ConflictError):
        engine.save(stale, etag=first.etag, approved=True)
    assert engine.pending()[0]["record"]["implementation_summary"] == "Stale revision"


def test_offline_queue_then_sync_and_rebuild(engine):
    record = task(engine)
    engine.save(record, approved=True, offline=True)
    assert engine.records() == []
    assert len(engine.pending()) == 1
    assert engine.sync(approved=True)["saved"] == [str(record.id)]
    with engine.db:
        engine.db.execute("DELETE FROM records")
        engine.db.execute("DELETE FROM search")
    assert engine.refresh() == 1
    assert engine.get(Kind.task, record.id).record.content_hash == record.content_hash


def test_remote_success_local_failure_reconciles_without_duplicate(engine, monkeypatch):
    record = task(engine)
    cache = engine._cache
    monkeypatch.setattr(engine, "_cache", lambda _: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        engine.save(record, approved=True)
    assert len(engine.provider.list_records()) == 1
    monkeypatch.setattr(engine, "_cache", cache)
    engine.sync(approved=True)
    assert len(engine.records()) == len(engine.provider.list_records()) == 1
    assert engine.pending() == []


def test_refresh_failure_preserves_prior_index(engine, monkeypatch):
    engine.save(task(engine), approved=True)

    def failed_list(*args):
        raise StorageError("partial enumeration")

    monkeypatch.setattr(engine.provider, "list_records", failed_list)
    with pytest.raises(StorageError):
        engine.refresh()
    assert len(engine.search("Kafka")) == 1


def test_refresh_cache_failure_rolls_back_all_rows(engine, monkeypatch):
    engine.save(task(engine), approved=True)
    cache_rows = engine._cache_rows

    def failed_cache(stored):
        cache_rows(stored)
        raise OSError("disk full")

    monkeypatch.setattr(engine, "_cache_rows", failed_cache)
    with pytest.raises(OSError):
        engine.refresh()
    assert len(engine.records()) == 1
    assert len(engine.search("Kafka")) == 1


def test_profile_isolation_and_switch_permission(engine):
    engine.save(task(engine), approved=True)
    other = Profile(name="other", account_type="mock")
    engine.config.add(other)
    with pytest.raises(ValueError):
        engine.config.switch("other")
    second = Engine(engine.config, other)
    assert second.records() == []
    assert second.pending() == []
    second.close()
    assert engine.config.active_name() == "demo"


def test_removed_profile_name_cannot_repurpose_retained_data(engine):
    engine.config.remove("demo", approved=True)
    with pytest.raises(ValueError, match="retired"):
        engine.config.add(Profile(name="demo", account_type="mock"))


def test_search_filters_and_explanations(engine):
    engine.save(task(engine), approved=True)
    engine.save(
        engine.prepare(
            {
                "kind": "debugging",
                "title": "Timeout investigation",
                "root_cause": "Connection pool exhaustion",
                "project_id": "api",
            }
        ),
        approved=True,
    )
    assert engine.search("Kafka", project="streaming")[0]["similarity"] == 1
    assert not engine.search("Kafka", project="api")
    assert not engine.search("root_cause")  # Schema field names are not knowledge.
    assert "Matching terms" in engine.search("connection")[0]["explanation"]
    assert not engine.search('" OR * ; DROP TABLE records; --', project="nonexistent")


def test_review_schedule_derived_from_history_no_question_write(engine):
    question = engine.prepare(
        {
            "kind": "question",
            "title": "Backpressure",
            "question": "Why pause consumption?",
            "expected_answer": "Bound memory",
            "created_at": (utcnow() - timedelta(days=2)).isoformat(),
        }
    )
    saved = engine.save(question, approved=True)
    assert engine.questions(due=True)[0]["question_id"] == str(question.id)
    assert "expected_answer" not in engine.questions()[0]
    engine.review(question.id, "Bound memory by pacing", 1, "Correct", approved=True)
    engine.review(question.id, "Bound memory", 0.9, "Correct", approved=True)
    state = engine.question_state(question)
    assert state["review_count"] == 2
    assert state["correct_count"] == 2
    assert state["confidence_score"] == 0.95
    assert not state["due"]
    assert engine.get(Kind.question, question.id, remote=True).etag == saved.etag
    engine.review(question.id, "Not sure", 0.1, "Review the mechanism", approved=True)
    state = engine.question_state(question)
    assert state["review_count"] == 3
    from datetime import datetime

    assert datetime.fromisoformat(state["next_review_at"]) < utcnow() + timedelta(days=2)


def test_approval_invalidated_when_profile_changes(engine):
    record = task(engine)
    first_hash = engine.preview(record)["approval_hash"]
    engine.profile = Profile(name="changed", account_type="mock")
    with pytest.raises(ValueError, match="hash"):
        engine.save(record, approved=True, expected_hash=first_hash)


def test_duplicate_explicit_ids_and_delete_versions(engine):
    first = engine.save(task(engine), approved=True)
    copied = first.record.model_copy(update={"id": uuid4()})
    second = engine.save(copied, approved=True)
    assert second.record.id == first.record.id
    with pytest.raises(ConflictError):
        engine.delete(Kind.task, first.record.id, "stale", approved=True)
    engine.delete(Kind.task, first.record.id, first.etag, approved=True)
    assert not engine.records()
    assert not engine.search("Kafka")


def test_audit_never_includes_payloads(engine):
    record = task(engine, description="password=do-not-save-this")
    engine.save(record, approved=True)
    payload = engine.get(Kind.task, record.id).record.description
    assert "do-not-save-this" not in payload
    audit = (engine.directory / "audit.jsonl").read_text()
    assert "description" not in audit and "do-not-save-this" not in audit


def test_unknown_status_does_not_count_as_done(engine):
    engine.save(task(engine), approved=True)
    assert engine.insights()["tasks_by_status"] == {"unknown": 1}


def test_dry_run_does_not_construct_auth_provider(engine, monkeypatch):
    import ekm.core

    monkeypatch.setattr(ekm.core, "make_provider", lambda *args: pytest.fail("auth initialized"))
    dry = Engine(engine.config, engine.profile)
    dry.preview(dry.prepare({"kind": "task", "title": "Draft"}))
    assert dry.records() == []
    dry.close()


def test_case_variant_profiles_cannot_share_cache(engine):
    with pytest.raises(ValueError, match="exists"):
        engine.config.add(Profile(name="DEMO", account_type="mock"))
    engine.config.remove("DEMO", approved=True)
    with pytest.raises(ValueError, match="retired"):
        engine.config.add(Profile(name="Demo", account_type="mock"))


def test_delete_remote_success_local_failure_cannot_resurrect_pending(engine):
    stored = engine.save(task(engine), approved=True)
    engine.save(stored.record, approved=True, offline=True)
    engine.db.execute("""CREATE TRIGGER fail_cache_delete BEFORE DELETE ON records
                       BEGIN SELECT RAISE(ABORT,'simulated disk error'); END""")
    import sqlite3

    with pytest.raises(sqlite3.DatabaseError):
        engine.delete(Kind.task, stored.record.id, stored.etag, approved=True)
    assert engine.provider.list_records() == []
    assert [p["operation"] for p in engine.pending()] == ["delete"]
    engine.db.execute("DROP TRIGGER fail_cache_delete")
    assert engine.sync(approved=True)["deleted"] == [str(stored.record.id)]
    assert engine.provider.list_records() == []
    assert engine.pending() == []
    with pytest.raises(ConflictError, match="deleted"):
        engine.save(stored.record, approved=True)
    engine.revive_deleted(stored.record.id, approved=True)
    engine.save(stored.record, approved=True)
    assert len(engine.records()) == 1


def test_reporting_projects_review_state_without_mutating_remote_question(engine):
    question = engine.prepare(
        {
            "kind": "question",
            "title": "Bound memory",
            "question": "Why?",
            "expected_answer": "Reliability",
        }
    )
    engine.save(question, approved=True)
    engine.review(question.id, "Reliability", 1, "Correct", approved=True)
    projected = next(r for r in engine.report_records() if r.id == question.id)
    assert projected.review_count == projected.correct_count == 1
    assert projected.confidence_score == 1
    assert projected.next_review_at is not None
    assert engine.get(Kind.question, question.id, remote=True).record.review_count == 0
