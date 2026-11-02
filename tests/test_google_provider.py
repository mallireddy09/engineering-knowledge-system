import copy
import json
import os
import re
from datetime import timedelta
from email import policy
from email.parser import BytesParser

import httpx
import pytest

from ekm.google.client import GoogleClient
from ekm.models import Kind, Record
from ekm.providers.base import ApprovalRequired, ConflictError, NotFoundError, StorageError
from ekm.providers.google_drive import FOLDER_MIME, GoogleDriveProvider


class Drive:
    def __init__(self):
        self.items = {
            "root": {
                "id": "root",
                "name": "My Drive",
                "mimeType": FOLDER_MIME,
                "parents": [],
                "appProperties": {},
                "version": "1",
                "trashed": False,
            }
        }
        self.contents = {}
        self.counter = 0
        self.fail_after_create = False
        self.calls = []
        self.page_size = 2

    def handler(self, request):
        self.calls.append(request)
        path = request.url.path
        if path.endswith("/files/generateIds"):
            self.counter += 1
            return httpx.Response(200, json={"ids": [f"file_{self.counter}"]})
        if path.endswith("/about"):
            return httpx.Response(200, json={"user": {"emailAddress": "test@example.com"}})
        if path.endswith("/files") and request.method == "GET":
            query = request.url.params["q"]
            parent = re.search(r"'([^']+)' in parents", query).group(1)
            props = re.findall(r"key='([^']+)' and value='([^']+)'", query)
            matches = [
                item
                for item in self.items.values()
                if not item.get("trashed")
                and parent in item.get("parents", [])
                and all(item.get("appProperties", {}).get(key) == value for key, value in props)
            ]
            start = int(request.url.params.get("pageToken", "0"))
            result = {"files": matches[start : start + self.page_size]}
            if start + self.page_size < len(matches):
                result["nextPageToken"] = str(start + self.page_size)
            return httpx.Response(200, json=result)
        if path.endswith("/files") and request.method == "POST":
            content = None
            if path.startswith("/upload/"):
                message = BytesParser(policy=policy.default).parsebytes(
                    (
                        "Content-Type: "
                        + request.headers["Content-Type"]
                        + "\r\nMIME-Version: 1.0\r\n\r\n"
                    ).encode()
                    + request.content
                )
                parts = list(message.iter_parts())
                metadata = json.loads(parts[0].get_payload(decode=True))
                content = parts[1].get_payload(decode=True)
            else:
                metadata = json.loads(request.content)
            id = metadata["id"]
            if id in self.items:
                return httpx.Response(409)
            self.items[id] = {**metadata, "version": "1", "trashed": False}
            if content is not None:
                self.contents[id] = content
            if self.fail_after_create:
                self.fail_after_create = False
                raise httpx.ReadError("lost result", request=request)
            return httpx.Response(201, json=self.items[id])
        id = path.rsplit("/", 1)[-1]
        if id not in self.items:
            return httpx.Response(404)
        if request.url.params.get("alt") == "media":
            return httpx.Response(200, content=self.contents[id])
        assert request.method == "GET", "provider must never mutate old Drive files"
        return httpx.Response(200, json=copy.deepcopy(self.items[id]))


@pytest.fixture
def provider(tmp_path):
    drive = Drive()
    client = GoogleClient(
        lambda: "secret", httpx.Client(transport=httpx.MockTransport(drive.handler))
    )
    provider = GoogleDriveProvider(
        client, app_id="desktop-client", account_id="subject", state_dir=tmp_path
    )
    provider.initialize_storage(approved=True)
    return drive, provider


def test_folder_initialization_reuse_owned_scope_and_permissions(provider, tmp_path):
    drive, storage = provider
    count = len(drive.items)
    assert count == 15
    storage.initialize_storage(approved=True)
    assert len(drive.items) == count
    assert storage.check_connection()["account"] == "test@example.com"
    other = GoogleDriveProvider(
        storage.client,
        storage.root_id,
        app_id="desktop-client",
        account_id="different",
        state_dir=tmp_path / "other",
    )
    with pytest.raises(StorageError, match="ownership"):
        other.list_records()
    with pytest.raises(ApprovalRequired):
        storage.create_record(Record(kind=Kind.task, title="Need approval"))
    if os.name != "nt":
        assert (tmp_path / "google-drive-operations.json").stat().st_mode & 0o777 == 0o600


def test_record_create_update_tombstone_recovery_and_export(provider, tmp_path):
    drive, storage = provider
    record = Record(kind=Kind.task, title="First task").sealed()
    first = storage.create_record(record, approved=True)
    assert storage.create_record(record, approved=True) == first
    changed = record.model_copy(update={"description": "Changed", "content_hash": ""}).sealed()
    second = storage.update_record(changed, first.etag, approved=True)
    assert first.etag != second.etag
    with pytest.raises(ConflictError):
        storage.update_record(record, first.etag, approved=True)
    assert storage.export_data()[0]["description"] == "Changed"
    assert storage.list_record_metadata()[0]["etag"] == second.etag
    recovered = GoogleDriveProvider(
        storage.client,
        storage.root_id,
        app_id="desktop-client",
        account_id="subject",
        state_dir=tmp_path / "new-machine",
    )
    assert recovered.get_record(record.kind, record.id).record == changed
    storage.delete_record(record.kind, record.id, second.etag, approved=True)
    with pytest.raises(NotFoundError):
        storage.get_record(record.kind, record.id)
    assert storage.list_records() == []
    assert (
        len(
            [
                item
                for item in drive.items.values()
                if item.get("appProperties", {}).get("ekmRole") == "record"
            ]
        )
        == 3
    )
    assert not any(request.method in {"PATCH", "DELETE", "PUT"} for request in drive.calls)


def test_concurrent_branches_are_preserved_and_merge_requires_all_heads(provider):
    _, storage = provider
    original = Record(kind=Kind.task, title="Concurrent task").sealed()
    first = storage.create_record(original, approved=True)
    left = original.model_copy(update={"description": "Left", "content_hash": ""}).sealed()
    right = original.model_copy(update={"description": "Right", "content_hash": ""}).sealed()
    storage._append(left, [first.etag], False)
    storage._append(right, [first.etag], False)
    with pytest.raises(ConflictError, match="divergent"):
        storage.get_record(original.kind, original.id)
    conflict = storage.conflicts()[0]
    assert {branch["record"]["description"] for branch in conflict["branches"]} == {"Left", "Right"}
    with pytest.raises(ConflictError, match="heads changed"):
        storage.resolve_conflict(
            original.kind, original.id, conflict["heads"][:1], left, approved=True
        )
    merged = original.model_copy(
        update={"description": "Merged evidence", "content_hash": ""}
    ).sealed()
    result = storage.resolve_conflict(
        original.kind, original.id, conflict["heads"], merged, approved=True
    )
    assert result.record.description == "Merged evidence"
    assert storage.conflicts() == []


def test_conflict_tombstone_merge_is_explicit(provider):
    _, storage = provider
    original = Record(kind=Kind.task, title="Delete versus update").sealed()
    first = storage.create_record(original, approved=True)
    storage._append(original, [first.etag], True)
    changed = original.model_copy(
        update={"description": "Other writer", "content_hash": ""}
    ).sealed()
    storage._append(changed, [first.etag], False)
    heads = storage.conflicts()[0]["heads"]
    assert storage.resolve_conflict(original.kind, original.id, heads, None, approved=True) is None
    with pytest.raises(NotFoundError):
        storage.get_record(original.kind, original.id)


def test_interrupted_create_reuses_persisted_file_id(provider, tmp_path):
    drive, storage = provider
    record = Record(kind=Kind.task, title="Ambiguous create").sealed()
    drive.fail_after_create = True
    with pytest.raises(StorageError, match="reconcile"):
        storage.create_record(record, approved=True)
    assert storage.create_record(record, approved=True).record == record
    revisions = [
        item
        for item in drive.items.values()
        if item.get("appProperties", {}).get("ekmRole") == "record"
    ]
    assert len(revisions) == 1


def test_pagination_lists_all_records_and_artifacts_never_overwrite(provider, tmp_path):
    drive, storage = provider
    for index in range(5):
        storage.create_record(Record(kind=Kind.task, title=f"Task {index}").sealed(), approved=True)
    assert len(storage.list_records()) == 5
    assert any("pageToken" in request.url.params for request in drive.calls)
    file = tmp_path / "journal.docx"
    file.write_bytes(b"first document")
    url = storage.upload_document(file, approved=True)
    assert storage.upload_document(file, approved=True) == url
    file.write_bytes(b"different")
    with pytest.raises(ConflictError):
        storage.upload_document(file, approved=True)


def test_shortcut_moved_root_and_corrupt_payload_fail_closed(provider):
    drive, storage = provider
    task = Record(kind=Kind.task, title="Protected").sealed()
    storage.create_record(task, approved=True)
    revision = next(
        item
        for item in drive.items.values()
        if item.get("appProperties", {}).get("ekmRole") == "record"
    )
    content = json.loads(drive.contents[revision["id"]])
    content["record"]["description"] = "Corrupted"
    drive.contents[revision["id"]] = json.dumps(content).encode()
    with pytest.raises(StorageError, match="invalid"):
        storage.get_record(task.kind, task.id)
    drive.items[storage.root_id]["parents"] = ["other-root"]
    with pytest.raises(StorageError, match="outside"):
        storage.list_records()
    drive.items[storage.root_id]["parents"] = ["root"]
    database = next(
        item
        for item in drive.items.values()
        if item.get("appProperties", {}).get("ekmRole") == "database"
    )
    database["mimeType"] = "application/vnd.google-apps.shortcut"
    with pytest.raises(StorageError, match="ownership"):
        storage.list_records()


def test_duplicate_folders_and_native_preallocated_ids_are_not_used(provider):
    drive, storage = provider
    folder = next(
        item
        for item in drive.items.values()
        if item.get("appProperties", {}).get("ekmRole") == "journals"
    )
    drive.items["duplicate"] = {**folder, "id": "duplicate"}
    with pytest.raises(ConflictError, match="Multiple"):
        storage.ensure_folder("journals")
    with pytest.raises(StorageError, match="limits"):
        storage.owner_properties("journal", huge="x" * 124)
    with pytest.raises(StorageError, match="Native"):
        storage._mapped_create("native", {"mimeType": "application/vnd.google-apps.document"})


def test_replay_immutable_revision_verifies_existing_preallocated_bytes(provider):
    drive, storage = provider
    original = Record(kind=Kind.task, title="Replay verified bytes").sealed()
    first = storage.create_record(original, approved=True)
    storage._append(original, [], False)
    assert storage.get_record(original.kind, original.id) == first
    revisions = [
        item
        for item in drive.items.values()
        if item.get("appProperties", {}).get("ekmRole") == "record"
    ]
    assert len(revisions) == 1
    # Deliberate damage must not turn an ID-conflict replay into an overwrite.
    drive.contents[revisions[0]["id"]] = b"different"
    with pytest.raises(ConflictError, match="content differs"):
        storage._append(original, [], False)


def test_exact_duplicate_logical_revisions_collapse_without_selecting_a_branch(provider):
    drive, storage = provider
    record = Record(kind=Kind.task, title="Duplicate identical logical revision").sealed()
    original = storage.create_record(record, approved=True)
    item = next(
        item
        for item in drive.items.values()
        if item.get("appProperties", {}).get("ekmRole") == "record"
    )
    drive.items["duplicate_blob"] = {**copy.deepcopy(item), "id": "duplicate_blob"}
    drive.contents["duplicate_blob"] = drive.contents[item["id"]]
    assert storage.get_record(record.kind, record.id) == original
    assert storage.conflicts() == []


def test_existing_unrelated_named_root_is_never_claimed(tmp_path):
    drive = Drive()
    drive.items["unrelated"] = {
        "id": "unrelated",
        "name": "EngineeringKnowledge",
        "mimeType": FOLDER_MIME,
        "parents": ["root"],
        "appProperties": {},
        "version": "1",
    }
    client = GoogleClient(
        lambda: "secret", httpx.Client(transport=httpx.MockTransport(drive.handler))
    )
    storage = GoogleDriveProvider(
        client, app_id="desktop-client", account_id="subject", state_dir=tmp_path
    )
    storage.initialize_storage(approved=True)
    assert storage.root_id != "unrelated"
    assert drive.items["unrelated"]["appProperties"] == {}


def test_record_missing_parent_and_identity_mismatch_are_rejected(provider):
    drive, storage = provider
    record = Record(kind=Kind.task, title="Missing history parent").sealed()
    storage._append(record, ["a" * 64], False)
    with pytest.raises(StorageError, match="missing parents"):
        storage.get_record(record.kind, record.id)
    with pytest.raises(StorageError, match="identifier"):
        storage.verify_file("../private", "root")


def test_simultaneous_identical_revision_with_different_timestamps_is_one_logical_record(
    provider, tmp_path
):
    drive, storage = provider
    original = Record(kind=Kind.task, title="Equivalent independent writer").sealed()
    first = storage.create_record(original, approved=True)
    newer = original.model_copy(
        update={
            "created_at": original.created_at + timedelta(days=1),
            "updated_at": original.updated_at + timedelta(days=1),
        }
    ).sealed()
    assert newer.content_hash == original.content_hash
    second_machine = GoogleDriveProvider(
        storage.client,
        storage.root_id,
        app_id="desktop-client",
        account_id="subject",
        state_dir=tmp_path / "second-machine",
    )
    # Both writers had observed no root revision before either append completed.
    second_machine._append(newer, [], False)
    assert (
        len(
            [
                item
                for item in drive.items.values()
                if item.get("appProperties", {}).get("ekmRole") == "record"
            ]
        )
        == 2
    )
    result = storage.get_record(original.kind, original.id)
    assert result.etag == first.etag and result.record == original
    assert storage.conflicts() == []
