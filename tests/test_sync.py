"""Download queue recovery and explicit conflict resolution without Microsoft access."""

from uuid import uuid4

import pytest

from ekm.config import Config, Profile
from ekm.core import Engine
from ekm.models import Kind, Record
from ekm.providers.base import ConflictError, StorageError
from ekm.providers.mock import MockStorageProvider


class QueuedProvider(MockStorageProvider):
    def list_record_metadata(self):
        return [
            {
                "id": str(s.record.id),
                "kind": s.record.kind.value,
                "etag": s.etag,
                "content_hash": s.record.content_hash,
            }
            for s in self.list_records()
        ]


@pytest.fixture
def engine(tmp_path):
    config = Config(tmp_path / "private")
    profile = Profile(name="demo", account_type="mock")
    config.add(profile)
    config.switch("demo", approved=True)
    provider = QueuedProvider(tmp_path / "remote")
    provider.initialize_storage(approved=True)
    result = Engine(config, profile, provider)
    yield result
    result.close()


def record(title):
    return Record(kind="task", title=title).sealed()


def test_sync_status_offline_does_not_access_remote(engine, monkeypatch):
    engine.save(record("Offline insight"), approved=True, offline=True)
    monkeypatch.setattr(engine.provider, "list_record_metadata", lambda: pytest.fail("remote call"))
    status = engine.sync_status()
    assert len(status["upload_queue"]) == 1
    assert status["download_queue"] == []
    assert status["remote_checked"] is False


def test_interrupted_download_does_not_publish_partial_cache_and_resumes(engine, monkeypatch):
    first, second = record("First"), record("Second")
    engine.provider.create_record(first, approved=True)
    engine.provider.create_record(second, approved=True)
    original = engine.provider.get_record
    metadata = engine.provider.list_record_metadata()
    # Keep enumeration independent of download failure, as actual OneDrive metadata listings are.
    monkeypatch.setattr(engine.provider, "list_record_metadata", lambda: metadata)

    def fail_one(kind, record_id):
        if record_id == second.id:
            raise StorageError("network offline")
        return original(kind, record_id)

    monkeypatch.setattr(engine.provider, "get_record", fail_one)
    result = engine.pull()
    assert result["failed"] == [str(second.id)]
    assert engine.records() == []
    queue = engine.sync_status()["download_queue"]
    assert {r["state"] for r in queue} == {"ready", "pending"}
    calls = []

    def restored(kind, record_id):
        calls.append(record_id)
        return original(kind, record_id)

    monkeypatch.setattr(engine.provider, "get_record", restored)
    assert engine.pull()["downloaded"] == 2
    assert calls == [second.id]  # Completed validated downloads survive the interruption.
    assert len(engine.records()) == 2
    assert not engine.sync_status()["download_queue"]


def test_remote_change_after_listing_queues_conflict_then_retries(engine, monkeypatch):
    initial = engine.provider.create_record(record("Old"), approved=True)
    metadata = engine.provider.list_record_metadata()
    updated = initial.record.model_copy(update={"title": "New"}).sealed()
    engine.provider.update_record(updated, initial.etag, approved=True)
    original = engine.provider.list_record_metadata
    monkeypatch.setattr(engine.provider, "list_record_metadata", lambda: metadata)
    assert engine.pull()["conflicts"] == [str(updated.id)]
    assert not engine.records()
    monkeypatch.setattr(engine.provider, "list_record_metadata", original)
    assert engine.pull()["downloaded"] == 1
    assert engine.records()[0].title == "New"


def test_remote_listing_failure_does_not_prune_cached_records(engine, monkeypatch):
    engine.save(record("Retain"), approved=True)

    def failed():
        raise StorageError("partial page")

    monkeypatch.setattr(engine.provider, "list_record_metadata", failed)
    with pytest.raises(StorageError):
        engine.pull()
    assert engine.records()[0].title == "Retain"


def test_full_snapshot_prunes_absent_remote_but_preserves_pending_work(engine):
    first = engine.save(record("Removed remotely"), approved=True)
    protected = engine.save(record("Pending change"), approved=True)
    engine.save(protected.record, approved=True, offline=True)
    engine.provider.delete_record(first.record.kind, first.record.id, first.etag, approved=True)
    engine.provider.delete_record(
        protected.record.kind, protected.record.id, protected.etag, approved=True
    )
    assert engine.pull()["downloaded"] == 0
    assert [r.id for r in engine.records()] == [protected.record.id]
    assert len(engine.pending()) == 1


def test_resolve_requires_current_etag_and_preserves_old_draft(engine):
    initial = engine.save(record("Initial"), approved=True)
    local = initial.record.model_copy(update={"title": "Local"}).sealed()
    engine.save(local, etag=initial.etag, offline=True, approved=True)
    remote = initial.record.model_copy(update={"title": "Remote"}).sealed()
    current = engine.provider.update_record(remote, initial.etag, approved=True)
    preview = engine.resolve(Kind.task, local.id, strategy="remote", dry_run=True)
    assert preview["remote"]["title"] == "Remote"
    assert len(engine.pending()) == 1
    with pytest.raises(ValueError, match="approval"):
        engine.resolve(Kind.task, local.id, strategy="remote", expected_etag=current.etag)
    with pytest.raises(ConflictError):
        engine.resolve(
            Kind.task, local.id, strategy="remote", expected_etag=initial.etag, approved=True
        )
    engine.resolve(
        Kind.task, local.id, strategy="remote", expected_etag=current.etag, approved=True
    )
    assert not engine.pending()
    assert engine.records()[0].title == "Remote"
    previous = engine.db.execute("SELECT payload FROM resolution_backups").fetchone()[0]
    assert Record.model_validate_json(previous).title == "Local"


def test_merged_resolution_is_etag_guarded_and_failure_retains_replacement(engine, monkeypatch):
    initial = engine.save(record("Base"), approved=True)
    local = initial.record.model_copy(update={"title": "Local change"}).sealed()
    engine.save(local, etag=initial.etag, approved=True, offline=True)
    merged = local.model_copy(update={"description": "Approved merge"}).model_dump(mode="json")

    def failed(*args, **kwargs):
        raise StorageError("offline again")

    monkeypatch.setattr(engine.provider, "update_record", failed)
    with pytest.raises(StorageError):
        engine.resolve(
            Kind.task,
            local.id,
            strategy="merge",
            merged=merged,
            expected_etag=initial.etag,
            approved=True,
        )
    assert engine.pending()[0]["record"]["description"] == "Approved merge"
    assert engine.db.execute("SELECT COUNT(*) FROM resolution_backups").fetchone()[0] == 1


def test_merge_cannot_change_record_identity(engine):
    initial = engine.save(record("Base"), approved=True)
    engine.save(initial.record, etag=initial.etag, approved=True, offline=True)
    changed = initial.record.model_copy(update={"id": uuid4()}).model_dump(mode="json")
    with pytest.raises(ValueError, match="same UUID"):
        engine.resolve(
            Kind.task,
            initial.record.id,
            strategy="merge",
            merged=changed,
            expected_etag=initial.etag,
            approved=True,
        )


def test_download_corrupt_payload_keeps_existing_cache(engine, monkeypatch):
    initial = engine.save(record("Verified"), approved=True)
    metadata = engine.provider.list_record_metadata()
    monkeypatch.setattr(engine.provider, "list_record_metadata", lambda: metadata)
    from ekm.providers.base import StoredRecord

    corrupt = initial.record.model_copy(update={"title": "Corruption"})
    monkeypatch.setattr(
        engine.provider, "get_record", lambda *args: StoredRecord(corrupt, initial.etag)
    )
    assert engine.pull()["failed"] == [str(initial.record.id)]
    assert engine.records()[0].title == "Verified"


@pytest.mark.parametrize("corruption", ["bad-hash", "different-id"])
def test_corrupted_ready_download_is_refetched(engine, monkeypatch, corruption):
    initial = engine.provider.create_record(record("Real remote"), approved=True)
    other = engine.provider.create_record(record("Temporary failure"), approved=True)
    metadata = engine.provider.list_record_metadata()
    getter = engine.provider.get_record
    monkeypatch.setattr(engine.provider, "list_record_metadata", lambda: metadata)

    def fail_one(kind, record_id):
        if record_id == other.record.id:
            raise StorageError("network failure")
        return getter(kind, record_id)

    monkeypatch.setattr(engine.provider, "get_record", fail_one)
    assert engine.pull()["failed"]
    changed = initial.record.model_copy(update={"title": "Corrupted staged data"})
    if corruption == "different-id":
        changed = changed.model_copy(update={"id": uuid4()}).sealed()
    with engine.db:
        engine.db.execute(
            "UPDATE downloads SET payload=? WHERE id=?",
            (changed.model_dump_json(), str(initial.record.id)),
        )
    monkeypatch.setattr(engine.provider, "get_record", getter)
    assert engine.pull()["downloaded"] == 2
    assert "Corrupted staged data" not in [r.title for r in engine.records()]
