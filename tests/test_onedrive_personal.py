import hashlib
import json
import os
from uuid import uuid4

import httpx
import pytest

from ekm.microsoft.graph_client import GraphClient, GraphHTTPError
from ekm.microsoft.onedrive_client import OneDriveClient
from ekm.models import Kind, Record
from ekm.providers.base import ApprovalRequired, ConflictError, StorageError
from ekm.providers.onedrive import OneDriveProvider


class Drive:
    """Small HTTP-level OneDrive simulator with IDs, conditional writes, and sessions."""

    def __init__(self):
        self.items = {"drive-root": {"id": "drive-root", "name": "", "folder": {}, "eTag": '"0"'}}
        self.parents = {}
        self.contents = {}
        self.sessions = {}
        self.counter = 0
        self.quota_failure = False
        self.lose_upload_response = False
        self.calls = []

    def add(self, parent, name, content=None):
        self.counter += 1
        id = str(self.counter)
        item = {
            "id": id,
            "name": name,
            "eTag": f'"{self.counter}"',
            "webUrl": f"https://onedrive.live.com/files/{id}",
        }
        item["folder" if content is None else "file"] = {}
        self.items[id] = item
        self.parents[id] = parent
        if content is not None:
            self.contents[id] = content
        return item

    def find(self, parent, name):
        return next(
            (
                item
                for id, item in self.items.items()
                if self.parents.get(id) == parent and item["name"].casefold() == name.casefold()
            ),
            None,
        )

    def resolve(self, path):
        tail = path.removeprefix("/me/drive/")
        if tail.startswith("root"):
            id, tail = "drive-root", tail[4:]
        else:
            tail = tail.removeprefix("items/")
            id, _, tail = tail.partition("/")
            if ":" in id:
                id, rest = id.split(":", 1)
                tail = rest + "/" + tail
        tail = tail.removeprefix(":").strip("/")
        if ":/" in tail:
            relative, action = tail.split(":/", 1)
        elif tail in {"children", "content", "createUploadSession"}:
            relative, action = "", tail
        else:
            relative, action = tail, ""
        parts = relative.strip("/").split("/") if relative.strip("/") else []
        for part in parts:
            child = self.find(id, part)
            if child is None:
                return None, action
            id = child["id"]
        return self.items.get(id), action

    def handler(self, request):
        self.calls.append(request)
        path = request.url.path.removeprefix("/v1.0")
        if request.url.host == "files.up.1drv.com":
            assert "authorization" not in request.headers
            parent, name = self.sessions[path]
            if self.quota_failure:
                return httpx.Response(507)
            if self.find(parent, name):
                return httpx.Response(409)
            item = self.add(parent, name, request.content)
            if self.lose_upload_response:
                self.lose_upload_response = False
                raise httpx.ReadError("lost upload response", request=request)
            return httpx.Response(201, json=item)
        if path == "/me":
            return httpx.Response(200, json={"id": "user", "displayName": "Personal user"})
        if path == "/me/drive":
            return httpx.Response(200, json={"id": "drive"})
        if path.endswith("createUploadSession"):
            parent_path = path.split(":/", 1)[0]
            parent, _ = self.resolve(parent_path)
            body = json.loads(request.content)
            name = body["item"]["name"]
            assert body["item"]["@microsoft.graph.conflictBehavior"] == "fail"
            if self.find(parent["id"], name):
                return httpx.Response(409)
            session = "/" + str(uuid4())
            self.sessions[session] = (parent["id"], name)
            return httpx.Response(200, json={"uploadUrl": "https://files.up.1drv.com" + session})
        item, action = self.resolve(path)
        if item is None:
            return httpx.Response(404)
        if action == "children":
            if request.method == "POST":
                body = json.loads(request.content)
                if self.find(item["id"], body["name"]):
                    return httpx.Response(409)
                return httpx.Response(201, json=self.add(item["id"], body["name"]))
            return httpx.Response(
                200,
                json={
                    "value": [
                        child
                        for id, child in self.items.items()
                        if self.parents.get(id) == item["id"]
                    ]
                },
            )
        if request.method in {"DELETE", "PUT"}:
            if request.headers.get("if-match") != item["eTag"]:
                return httpx.Response(412)
            if request.method == "DELETE":
                self.items.pop(item["id"])
                self.contents.pop(item["id"], None)
                return httpx.Response(204)
            self.counter += 1
            item["eTag"] = f'"{self.counter}"'
            self.contents[item["id"]] = request.content
            return httpx.Response(200, json=item)
        if action == "content":
            return httpx.Response(200, content=self.contents[item["id"]])
        return httpx.Response(200, json=item)


@pytest.fixture
def personal(tmp_path):
    drive = Drive()
    graph = GraphClient(
        lambda: "secret", httpx.Client(transport=httpx.MockTransport(drive.handler))
    )
    provider = OneDriveProvider(
        graph,
        location="drive-root",
        app_id="app",
        account_id="account",
        state_dir=tmp_path / "profile",
    )
    provider.initialize_storage(approved=True)
    return drive, graph, provider


def test_personal_root_has_bound_ownership_and_backups(personal):
    drive, _, provider = personal
    root = drive.find("drive-root", "EngineeringKnowledge")
    assert root["id"] == provider.root_id
    marker = drive.find(provider.root_id, "_ekm-owner.json")
    data = json.loads(drive.contents[marker["id"]])
    assert data["app_id"] == "app" and data["root_id"] == provider.root_id
    assert data["account_hash"] == hashlib.sha256(b"account").hexdigest()
    assert drive.find(provider.root_id, "backups")
    count = len(drive.items)
    provider.initialize_storage(approved=True)
    assert len(drive.items) == count


def test_existing_unowned_folder_requires_explicit_adoption(tmp_path):
    drive = Drive()
    root = drive.add("drive-root", "EngineeringKnowledge")
    graph = GraphClient(lambda: "token", httpx.Client(transport=httpx.MockTransport(drive.handler)))
    provider = OneDriveProvider(graph, location="drive-root", app_id="app", account_id="account")
    with pytest.raises(StorageError, match="adopt-existing"):
        provider.initialize_storage(approved=True)
    with pytest.raises(ApprovalRequired):
        provider.initialize_storage(adopt_existing=True)
    provider.initialize_storage(approved=True, adopt_existing=True)
    assert provider.root_id == root["id"]
    foreign = OneDriveProvider(
        graph, provider.root_id, location="drive-root", app_id="other", account_id="account"
    )
    with pytest.raises(StorageError, match="ownership"):
        foreign.initialize_storage(approved=True, adopt_existing=True)


def test_each_mutation_rechecks_owner_marker(personal, tmp_path):
    drive, _, provider = personal
    marker = drive.find(provider.root_id, "_ekm-owner.json")
    drive.contents[marker["id"]] = b"{}"
    draft = Record(kind=Kind.task, title="Must not publish")
    with pytest.raises(StorageError, match="ownership"):
        provider.create_record(draft, approved=True)
    file = tmp_path / "report.docx"
    file.write_bytes(b"document")
    with pytest.raises(StorageError, match="ownership"):
        provider.upload_file(file, approved=True)
    assert not any(item["name"] == "report.docx" for item in drive.items.values())


def test_document_upload_version_update_and_external_edits_fail_closed(personal, tmp_path):
    drive, _, provider = personal
    file = tmp_path / "dashboard.xlsx"
    file.write_bytes(b"original workbook")
    first = provider.upload_file(file, approved=True)
    assert provider.upload_file(file, approved=True)["etag"] == first["etag"]
    file.write_bytes(b"new EKM workbook")
    second = provider.update_document(file, first["etag"], approved=True)
    assert second["etag"] != first["etag"]
    with pytest.raises(ConflictError):
        provider.update_document(file, first["etag"], approved=True)
    drive.contents[second["id"]] = b"workbook with unrelated user sheet"
    drive.items[second["id"]]["eTag"] = '"user-change"'
    with pytest.raises(ConflictError, match="preserve user edits"):
        provider.update_document(file, '"user-change"', approved=True)
    assert drive.contents[second["id"]] == b"workbook with unrelated user sheet"
    with pytest.raises(ConflictError):
        provider.upload_file(file, approved=True)


def test_document_without_creation_ledger_cannot_be_updated(personal, tmp_path):
    drive, _, provider = personal
    folder = drive.find(provider.root_id, "journals")
    existing = drive.add(folder["id"], "manual.docx", b"original")
    file = tmp_path / "manual.docx"
    file.write_bytes(b"original")
    provider.upload_file(file, approved=True)
    with pytest.raises(StorageError, match="provenance"):
        provider.update_document(file, existing["eTag"], approved=True)


def test_temp_connection_deletes_only_its_unique_file(personal):
    drive, _, provider = personal
    folder = drive.find(provider.root_id, "manifests")
    other = drive.add(folder["id"], "keep-me.json", b"unrelated")
    result = provider.test_connection(approved=True)
    assert result["cleanup"] == "deleted"
    assert drive.items[other["id"]]["name"] == "keep-me.json"
    assert drive.find(folder["id"], result["temporary_file"]) is None


def test_temp_test_recovers_ambiguous_create_and_cleans_up(personal):
    drive, _, provider = personal
    drive.lose_upload_response = True
    with pytest.raises(StorageError, match="reconcile"):
        provider.test_connection(approved=True)
    assert not any(item["name"].startswith("ekm-test-") for item in drive.items.values())


def test_quota_failure_does_not_remove_user_files(personal, tmp_path):
    drive, _, provider = personal
    before = set(drive.items)
    drive.quota_failure = True
    file = tmp_path / "journal.docx"
    file.write_bytes(b"document")
    with pytest.raises(GraphHTTPError) as error:
        provider.upload_file(file, approved=True)
    assert error.value.status_code == 507
    assert set(drive.items) == before


def test_path_safe_client_download_backup_and_metadata_queue(personal, tmp_path):
    drive, graph, provider = personal
    client = OneDriveClient(graph, provider.root_id)
    for folder, name in [
        ("../", "file.json"),
        ("backups", "../x"),
        ("backups", "bad\n.json"),
        ("backups", "a%2fb.json"),
    ]:
        with pytest.raises(StorageError):
            client.metadata(folder, name)
    record = Record(kind=Kind.task, title="Safe backup").sealed()
    provider.create_record(record, approved=True)
    metadata = provider.list_record_metadata()
    assert metadata[0]["id"] == str(record.id) and metadata[0]["etag"]
    provider.backup_data([record.model_dump(mode="json")], "backup.json", approved=True)
    output = tmp_path / "backup.json"
    result = provider.download_document("backups", "backup.json", output)
    assert result["content_hash"] == hashlib.sha256(output.read_bytes()).hexdigest()
    if os.name != "nt":
        assert output.stat().st_mode & 0o777 == 0o600
    assert json.loads(output.read_text())["records"][0]["id"] == str(record.id)
    with pytest.raises(FileExistsError):
        provider.download_document("backups", "backup.json", output)


def test_folder_shortcuts_are_rejected_before_upload(personal, tmp_path):
    drive, _, provider = personal
    journals = drive.find(provider.root_id, "journals")
    journals["remoteItem"] = {"id": "unrelated-shared-folder"}
    file = tmp_path / "report.docx"
    file.write_bytes(b"private")
    before = len(drive.sessions)
    with pytest.raises(StorageError, match="shortcuts"):
        provider.upload_file(file, approved=True)
    assert len(drive.sessions) == before
    with pytest.raises(StorageError, match="collides"):
        provider.initialize_storage(approved=True)


def test_record_folder_shortcuts_block_reads_and_mutations(personal):
    drive, _, provider = personal
    record = Record(kind=Kind.task, title="Ordinary record").sealed()
    stored = provider.create_record(record, approved=True)
    database = drive.find(provider.root_id, "database")
    tasks = drive.find(database["id"], "tasks")
    tasks["remoteItem"] = {"id": "outside-folder"}
    before = len(drive.calls)
    for action in (
        lambda: provider.get_record(record.kind, record.id),
        lambda: provider.list_records(),
        provider.list_record_metadata,
        lambda: provider.create_record(record, approved=True),
        lambda: provider.update_record(record, stored.etag, approved=True),
        lambda: provider.delete_record(record.kind, record.id, stored.etag, approved=True),
    ):
        with pytest.raises(StorageError, match="shortcuts"):
            action()
    assert all(request.method == "GET" for request in drive.calls[before:])


def test_generic_record_read_checks_owner_marker(personal):
    drive, _, provider = personal
    record = Record(kind=Kind.task, title="Bound record").sealed()
    provider.create_record(record, approved=True)
    marker = drive.find(provider.root_id, "_ekm-owner.json")
    contents = json.loads(drive.contents[marker["id"]])
    contents["app_id"] = "different-application"
    drive.contents[marker["id"]] = json.dumps(contents).encode()
    with pytest.raises(StorageError, match="ownership"):
        provider.get_record(record.kind, record.id)


def test_document_update_rejects_malformed_provenance_safely(personal, tmp_path):
    _, _, provider = personal
    file = tmp_path / "journal.docx"
    file.write_bytes(b"document")
    uploaded = provider.upload_file(file, approved=True)
    ledger = provider.state_dir / "documents.json"
    for invalid in ("not-an-object", {"id": uploaded["id"]}):
        ledger.write_text(json.dumps({provider._ledger_key("journals", file.name): invalid}))
        with pytest.raises(StorageError, match="provenance is invalid"):
            provider.update_document(file, uploaded["etag"], approved=True)
