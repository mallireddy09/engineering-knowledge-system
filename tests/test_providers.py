import json
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import httpx
import pytest

from ekm.microsoft.graph_client import GraphClient
from ekm.models import Kind, Record
from ekm.providers.base import ApprovalRequired, ConflictError, NotFoundError, StorageError
from ekm.providers.mock import MockStorageProvider
from ekm.providers.onedrive import OneDriveProvider
from ekm.providers.sharepoint import SharePointProvider


def test_mock_durable_crud_and_isolation(tmp_path):
    provider = MockStorageProvider(tmp_path / "personal")
    with pytest.raises(ApprovalRequired):
        provider.initialize_storage()
    provider.initialize_storage(approved=True)
    record = Record(kind=Kind.task, title="Understand retry failure")
    with pytest.raises(ApprovalRequired):
        provider.create_record(record)
    stored = provider.create_record(record, approved=True)
    assert provider.create_record(record, approved=True).etag == stored.etag
    assert (
        MockStorageProvider(tmp_path / "personal").get_record(record.kind, record.id).record
        == record
    )
    company = MockStorageProvider(tmp_path / "company")
    company.initialize_storage(approved=True)
    assert company.list_records() == []
    changed = record.model_copy(update={"title": "Verified retry failure"})
    newer = provider.update_record(changed, stored.etag, approved=True)
    assert newer.etag != stored.etag
    with pytest.raises(ConflictError):
        provider.update_record(record, stored.etag, approved=True)
    with pytest.raises(ConflictError):
        provider.delete_record(record.kind, record.id, stored.etag, approved=True)
    assert len(provider.search_records("verified")) == 1
    provider.delete_record(record.kind, record.id, newer.etag, approved=True)
    with pytest.raises(NotFoundError):
        provider.get_record(record.kind, record.id)


def test_mock_concurrent_creation_never_overwrites(tmp_path):
    provider = MockStorageProvider(tmp_path)
    provider.initialize_storage(approved=True)
    id = uuid4()
    records = [Record(id=id, kind=Kind.task, title=f"Writer {i}") for i in range(2)]

    def save(record):
        try:
            provider.create_record(record, approved=True)
            return True
        except ConflictError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(save, records)) == [False, True]
    assert len(provider.list_records()) == 1


def test_mock_export_and_document_collision(tmp_path):
    provider = MockStorageProvider(tmp_path / "remote")
    provider.initialize_storage(approved=True)
    record = Record(kind=Kind.decision, title="Choose UUID identities")
    provider.create_record(record, approved=True)
    assert provider.export_data()[0]["id"] == str(record.id)
    document = tmp_path / "journal.docx"
    document.write_bytes(b"document")
    assert provider.upload_document(document, approved=True).endswith("journal.docx")
    with pytest.raises(ConflictError):
        provider.upload_document(document, approved=True)


def test_onedrive_read_before_init_cannot_create_folder():
    calls = []
    graph = GraphClient(
        lambda: "token", httpx.Client(transport=httpx.MockTransport(lambda r: calls.append(r)))
    )
    provider = OneDriveProvider(graph)
    with pytest.raises(StorageError, match="not initialized"):
        provider.list_records()
    with pytest.raises(ApprovalRequired):
        provider.initialize_storage()
    assert calls == []


def test_onedrive_crud_uses_etag_and_conflict_fail_upload():
    record = Record(kind=Kind.task, title="Document a failure")
    state = {"record": None, "etag": '"1"'}
    requests = []

    def handler(request):
        requests.append(request)
        path = request.url.path
        if request.method == "POST" and path.endswith("createUploadSession"):
            assert (
                json.loads(request.content)["item"]["@microsoft.graph.conflictBehavior"] == "fail"
            )
            return httpx.Response(200, json={"uploadUrl": "https://files.up.1drv.com/session"})
        if request.url.host == "files.up.1drv.com":
            assert "authorization" not in request.headers
            state["record"] = json.loads(request.content)
            return httpx.Response(201, json={"id": "file", "eTag": state["etag"]})
        if request.method == "GET" and path.endswith("/database/tasks"):
            return httpx.Response(200, json={"id": "tasks-folder"})
        if request.method == "GET" and path.endswith("/content"):
            return httpx.Response(200, json=state["record"])
        if request.method == "GET":
            if state["record"] is None:
                return httpx.Response(404)
            return httpx.Response(200, json={"id": "file", "eTag": state["etag"]})
        if request.method in ("PUT", "DELETE"):
            if request.headers.get("if-match") != state["etag"]:
                return httpx.Response(412)
            if request.method == "DELETE":
                state["record"] = None
                return httpx.Response(204)
            state["record"] = json.loads(request.content)
            state["etag"] = '"2"'
            return httpx.Response(200, json={"id": "file", "eTag": state["etag"]})
        raise AssertionError(request)

    graph = GraphClient(lambda: "token", httpx.Client(transport=httpx.MockTransport(handler)))
    provider = OneDriveProvider(graph, "root")
    stored = provider.create_record(record, approved=True)
    assert provider.get_record(record.kind, record.id).record == record
    assert provider.create_record(record, approved=True) == stored
    updated = record.model_copy(update={"title": "Verified failure"})
    latest = provider.update_record(updated, stored.etag, approved=True)
    assert latest.etag == '"2"'
    with pytest.raises(ConflictError):
        provider.update_record(record, stored.etag, approved=True)
    provider.delete_record(record.kind, record.id, latest.etag, approved=True)
    with pytest.raises(NotFoundError):
        provider.get_record(record.kind, record.id)
    assert all(
        r.headers.get("authorization") == "Bearer token"
        for r in requests
        if r.url.host == "graph.microsoft.com"
    )


def test_sharepoint_unique_id_and_payload_crud():
    record = Record(kind=Kind.decision, title="Use optimistic concurrency")
    state = {"item": None}

    def handler(request):
        path = request.url.path
        if path.endswith("/lists"):
            return httpx.Response(
                200, json={"value": [{"displayName": "EKM_Decisions", "id": "list"}]}
            )
        if request.method == "GET":
            return httpx.Response(200, json={"value": [state["item"]] if state["item"] else []})
        if request.method == "POST":
            fields = json.loads(request.content)["fields"]
            assert fields["EKMId"] == str(record.id)
            state["item"] = {"id": "1", "eTag": '"1"', "fields": fields}
            return httpx.Response(201, json=state["item"])
        if request.method in ("PATCH", "DELETE"):
            if request.headers.get("if-match") != state["item"]["eTag"]:
                return httpx.Response(412)
            if request.method == "DELETE":
                state["item"] = None
                return httpx.Response(204)
            state["item"] = {"id": "1", "eTag": '"2"', "fields": json.loads(request.content)}
            return httpx.Response(200, json=state["item"]["fields"])
        raise AssertionError(request)

    graph = GraphClient(lambda: "token", httpx.Client(transport=httpx.MockTransport(handler)))
    provider = SharePointProvider(graph, "tenant,site,web")
    stored = provider.create_record(record, approved=True)
    assert provider.get_record(record.kind, record.id).record == record
    assert provider.create_record(record, approved=True) == stored
    updated = record.model_copy(update={"title": "ETag guarded updates"})
    latest = provider.update_record(updated, stored.etag, approved=True)
    with pytest.raises(ConflictError):
        provider.delete_record(record.kind, record.id, stored.etag, approved=True)
    provider.delete_record(record.kind, record.id, latest.etag, approved=True)


def test_sharepoint_payload_limits_and_approval():
    provider = SharePointProvider(None, "site")
    with pytest.raises(ApprovalRequired):
        provider.initialize_storage()
    with pytest.raises(ApprovalRequired):
        provider.create_record(Record(kind=Kind.task, title="Test"))
    with pytest.raises(StorageError, match="60,000"):
        provider._fields(Record(kind=Kind.task, title="Large", description="a" * 60000))
    identity = next(column for column in provider._columns() if column["name"] == "EKMId")
    assert identity["enforceUniqueValues"] and identity["indexed"]


def test_onedrive_rejects_malformed_remote_identity():
    requested_id = uuid4()
    wrong = Record(kind=Kind.task, title="Other identity")

    def handler(request):
        if request.url.path.endswith("/content"):
            return httpx.Response(200, json=wrong.model_dump(mode="json"))
        return httpx.Response(200, json={"id": "file", "eTag": '"1"'})

    graph = GraphClient(lambda: "token", httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(StorageError, match="identity"):
        OneDriveProvider(graph, "root").get_record(Kind.task, requested_id)


def test_sharepoint_pagination_and_duplicate_stable_identity():
    record = Record(kind=Kind.task, title="First page")
    another = Record(kind=Kind.task, title="Next page")

    def item(value, id):
        return {
            "id": id,
            "eTag": '"1"',
            "fields": {"EKMId": str(value.id), "EKMPayload": value.model_dump_json()},
        }

    def handler(request):
        path = request.url.path
        if path.endswith("/lists"):
            return httpx.Response(200, json={"value": [{"displayName": "EKM_Tasks", "id": "list"}]})
        if "$filter" in request.url.params:
            return httpx.Response(200, json={"value": [item(record, "1"), item(record, "2")]})
        if "page" in request.url.params:
            return httpx.Response(200, json={"value": [item(another, "2")]})
        return httpx.Response(
            200,
            json={
                "value": [item(record, "1")],
                "@odata.nextLink": "https://graph.microsoft.com/v1.0/sites/site/lists/list/items?page=2",
            },
        )

    graph = GraphClient(lambda: "token", httpx.Client(transport=httpx.MockTransport(handler)))
    provider = SharePointProvider(graph, "site")
    assert [stored.record.title for stored in provider.list_records(Kind.task)] == [
        "First page",
        "Next page",
    ]
    with pytest.raises(ConflictError, match="Duplicate"):
        provider.get_record(Kind.task, record.id)


def test_sharepoint_rejects_payload_identity_mismatch():
    record = Record(kind=Kind.task, title="Mismatch")
    item = {
        "id": "1",
        "eTag": '"1"',
        "fields": {"EKMId": str(uuid4()), "EKMPayload": record.model_dump_json()},
    }
    with pytest.raises(StorageError, match="identity"):
        SharePointProvider._stored(item, Kind.task)


def test_onedrive_initialize_reuse_list_export_and_document_upload(tmp_path):
    record = Record(kind=Kind.task, title="Recoverable JSON")
    folders = {}
    creations = []
    uploads = []

    def handler(request):
        path = request.url.path.removeprefix("/v1.0")
        if request.url.host == "files.up.1drv.com":
            assert "authorization" not in request.headers
            uploads.append(request.content)
            return httpx.Response(
                201, json={"id": "document", "webUrl": "https://example.com/journal"}
            )
        if path == "/me/drive":
            return httpx.Response(200, json={"id": "drive"})
        if path == "/me/drive/special/approot":
            return httpx.Response(200, json={"id": "root"})
        if path.endswith("/children") and ":/database/" not in path:
            if request.method == "POST":
                body = json.loads(request.content)
                assert body["@microsoft.graph.conflictBehavior"] == "fail"
                item = {"id": body["name"], "name": body["name"], "folder": {}}
                folders.setdefault(path, []).append(item)
                creations.append(item)
                return httpx.Response(201, json=item)
            return httpx.Response(200, json={"value": folders.get(path, [])})
        if path.endswith("createUploadSession"):
            return httpx.Response(200, json={"uploadUrl": "https://files.up.1drv.com/session"})
        if path.endswith("/journals"):
            return httpx.Response(200, json={"id": "journals"})
        if path.endswith("/dashboards"):
            return httpx.Response(200, json={"id": "dashboards"})
        if path.endswith("/content"):
            return httpx.Response(200, json=record.model_dump(mode="json"))
        if path.endswith("/database/tasks:/children"):
            return httpx.Response(
                200,
                json={
                    "value": [
                        {"id": "file", "name": f"{record.id}.json", "file": {}},
                        {"id": "notes", "name": "readme.txt", "file": {}},
                    ]
                },
            )
        if path.endswith(":/children"):
            return httpx.Response(200, json={"value": []})
        if path.endswith(f"/{record.id}.json"):
            return httpx.Response(200, json={"id": "file", "eTag": '"1"'})
        raise AssertionError(request)

    graph = GraphClient(lambda: "token", httpx.Client(transport=httpx.MockTransport(handler)))
    provider = OneDriveProvider(graph)
    assert provider.check_connection()["initialized"] is False
    provider.initialize_storage(approved=True)
    assert provider.root_id == "root"
    count = len(creations)
    assert count == 12
    provider.initialize_storage(approved=True)
    assert len(creations) == count
    assert provider.list_records()[0].record == record
    assert provider.export_data()[0]["id"] == str(record.id)
    assert provider.check_connection()["initialized"] is True
    document = tmp_path / "journal.docx"
    document.write_bytes(b"DOCX bytes")
    assert provider.upload_document(document, approved=True) == "https://example.com/journal"
    dashboard = tmp_path / "dashboard.xlsx"
    dashboard.write_bytes(b"XLSX bytes")
    provider.upload_document(dashboard, approved=True)
    assert uploads == [b"DOCX bytes", b"XLSX bytes"]


def test_onedrive_initializer_refuses_existing_file_collision():
    def handler(request):
        if request.url.path.endswith("approot"):
            return httpx.Response(200, json={"id": "root"})
        return httpx.Response(200, json={"value": [{"name": "database", "id": "file", "file": {}}]})

    graph = GraphClient(lambda: "token", httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(StorageError, match="non-folder"):
        OneDriveProvider(graph).initialize_storage(approved=True)


def test_sharepoint_initialize_reuses_lists_and_uploads_documents(tmp_path):
    lists = []
    folder_created = False
    incompatible_schema = False
    mutations = []

    def handler(request):
        nonlocal folder_created
        path = request.url.path.removeprefix("/v1.0")
        if request.url.host == "files.up.1drv.com":
            assert "authorization" not in request.headers
            assert request.content == b"report"
            return httpx.Response(
                201, json={"id": "report", "webUrl": "https://example.com/report"}
            )
        if request.method != "GET":
            mutations.append(request.method)
        if path == "/sites/site":
            return httpx.Response(200, json={"id": "site"})
        if path == "/sites/site/lists":
            if request.method == "POST":
                body = json.loads(request.content)
                item = {"id": body["displayName"], "displayName": body["displayName"]}
                lists.append(item)
                return httpx.Response(201, json=item)
            return httpx.Response(200, json={"value": lists})
        if path.endswith("/columns"):
            columns = SharePointProvider._columns()
            if incompatible_schema:
                columns[0]["enforceUniqueValues"] = False
            return httpx.Response(200, json={"value": columns})
        if path == "/sites/site/drive":
            return httpx.Response(200, json={"id": "drive"})
        if path == "/drives/drive/root/children":
            item = {"id": "reports", "name": "EngineeringKnowledge", "folder": {}}
            if request.method == "POST":
                folder_created = True
                return httpx.Response(201, json=item)
            return httpx.Response(200, json={"value": [item] if folder_created else []})
        if path.endswith("createUploadSession"):
            body = json.loads(request.content)
            assert body["item"]["@microsoft.graph.conflictBehavior"] == "fail"
            return httpx.Response(200, json={"uploadUrl": "https://files.up.1drv.com/session"})
        raise AssertionError(request)

    graph = GraphClient(lambda: "token", httpx.Client(transport=httpx.MockTransport(handler)))
    provider = SharePointProvider(graph, "site")
    assert provider.check_connection()["connected"] is True
    provider.initialize_storage(approved=True)
    assert len(lists) == 7 and folder_created
    assert len(mutations) == 8
    provider.initialize_storage(approved=True)
    assert len(mutations) == 8
    document = tmp_path / "report.xlsx"
    document.write_bytes(b"report")
    assert provider.upload_document(document, approved=True) == "https://example.com/report"
    incompatible_schema = True
    with pytest.raises(StorageError, match="unique indexed"):
        provider.initialize_storage(approved=True)


def test_sharepoint_initialize_does_not_modify_ambiguous_lists():
    def handler(request):
        assert request.method == "GET"
        return httpx.Response(
            200,
            json={
                "value": [
                    {"id": "1", "displayName": "EKM_Tasks"},
                    {"id": "2", "displayName": "EKM_Tasks"},
                ]
            },
        )

    graph = GraphClient(lambda: "token", httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(StorageError, match="Duplicate"):
        SharePointProvider(graph, "site").initialize_storage(approved=True)


def test_providers_reject_corrupted_sealed_records(tmp_path):
    original = Record(kind=Kind.task, title="Verified content", description="Original").sealed()
    corrupted = original.model_dump(mode="json")
    corrupted["description"] = "Accidentally corrupted"
    mock = MockStorageProvider(tmp_path)
    mock.initialize_storage(approved=True)
    mock.create_record(original, approved=True)
    (tmp_path / "task" / f"{original.id}.json").write_text(json.dumps(corrupted))
    with pytest.raises(StorageError, match="invalid"):
        mock.get_record(original.kind, original.id)
    item = {
        "id": "1",
        "eTag": '"1"',
        "fields": {"EKMId": str(original.id), "EKMPayload": json.dumps(corrupted)},
    }
    with pytest.raises(StorageError, match="invalid"):
        SharePointProvider._stored(item, Kind.task)

    def handler(request):
        if request.url.path.endswith("/content"):
            return httpx.Response(200, json=corrupted)
        return httpx.Response(200, json={"id": "file", "eTag": '"1"'})

    graph = GraphClient(lambda: "token", httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(StorageError, match="invalid"):
        OneDriveProvider(graph, "root").get_record(original.kind, original.id)
