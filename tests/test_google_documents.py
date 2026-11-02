"""Native API tests: append-only publication, revision guards, and shared content."""

import copy
import re
from types import SimpleNamespace

import pytest

from ekm.google.documents import DOC_MIME, DOCS, DRIVE, SHEETS, GoogleDocuments
from ekm.models import Record
from ekm.providers.base import ApprovalRequired, ConflictError, StorageError


class NativeClient:
    def __init__(self):
        self.files = {}
        self.documents = {}
        self.spreadsheets = {}
        self.calls = []
        self.fail_before_batch = False
        self.fail_after_batch = False
        self.fail_create_after_commit = False

    def iter_files(self, query, fields=None):
        properties = re.findall(r"key='([^']+)' and value='([^']+)'", query)
        parent = re.search(r"'([^']+)' in parents", query).group(1)
        return [
            item
            for item in self.files.values()
            if item["parents"] == [parent]
            and all(item["appProperties"].get(k) == v for k, v in properties)
        ]

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, copy.deepcopy(kwargs)))
        body = kwargs.get("json", {})
        if path == DRIVE and method == "POST":
            file_id = f"native-{len(self.files) + 1}"
            item = {**body, "id": file_id}
            self.files[file_id] = item
            if item["mimeType"] == DOC_MIME:
                self.documents[file_id] = {
                    "revisionId": "rev-1",
                    "namedRanges": {},
                    "body": {"content": [{"endIndex": 2}]},
                }
            else:
                self.spreadsheets[file_id] = {
                    "spreadsheetId": file_id,
                    "sheets": [],
                    "developerMetadata": [],
                }
            if self.fail_create_after_commit:
                self.fail_create_after_commit = False
                raise StorageError("response lost")
            return SimpleNamespace(json=lambda: copy.deepcopy(item))
        file_id = path.split("/")[-1].removesuffix(":batchUpdate")
        if path.startswith(DOCS):
            doc = self.documents[file_id]
            if method == "GET":
                return SimpleNamespace(json=lambda: copy.deepcopy(doc))
            if self.fail_before_batch:
                self.fail_before_batch = False
                raise StorageError("batch did not complete")
            assert body["writeControl"] == {"requiredRevisionId": doc["revisionId"]}
            for request in body["requests"]:
                if "insertText" in request:
                    text = request["insertText"]["text"]
                    doc.setdefault("text", "")
                    doc["text"] += text
                    doc["body"]["content"][-1]["endIndex"] += len(text.encode("utf-16-le")) // 2
                if "createNamedRange" in request:
                    marker = request["createNamedRange"]
                    doc["namedRanges"][marker["name"]] = {
                        "namedRanges": [{"ranges": [marker["range"]]}]
                    }
            doc["revisionId"] += "+1"
            if self.fail_after_batch:
                self.fail_after_batch = False
                raise StorageError("response lost after commit")
            return SimpleNamespace(
                json=lambda: {"writeControl": {"requiredRevisionId": doc["revisionId"]}}
            )
        if path.startswith(SHEETS):
            spreadsheet = self.spreadsheets[file_id]
            if method == "GET":
                return SimpleNamespace(json=lambda: copy.deepcopy(spreadsheet))
            if self.fail_before_batch:
                self.fail_before_batch = False
                raise StorageError("batch did not complete")
            for request in body["requests"]:
                if "addSheet" in request:
                    spreadsheet["sheets"].append(request["addSheet"])
                if "createDeveloperMetadata" in request:
                    spreadsheet["developerMetadata"].append(
                        request["createDeveloperMetadata"]["developerMetadata"]
                    )
            if self.fail_after_batch:
                self.fail_after_batch = False
                raise StorageError("response lost after commit")
            return SimpleNamespace(json=lambda: {})
        raise AssertionError(path)


class NativeProvider:
    def __init__(self, state_dir):
        self.state_dir = state_dir
        self.client = NativeClient()

    def ensure_folder(self, role, *, approved=False):
        assert approved
        return role + "-folder"

    def owner_properties(self, role, **extra):
        return {"ekmOwner": "owner-hash", "ekmRole": role, **extra}

    def verify_file(self, file_id, role, parent_id=None):
        item = self.client.files[file_id]
        if item["parents"] != [parent_id] or item["appProperties"]["ekmRole"] != role:
            raise StorageError("foreign file")
        return item


@pytest.fixture
def native(tmp_path):
    provider = NativeProvider(tmp_path / "private")
    return provider, GoogleDocuments(provider)


def task(**changes):
    return Record(
        kind="task",
        title="Bound consumer retries 🚀",
        task_id="DEMO-1",
        description="Keep memory bounded",
        implementation_summary="Backpressure",
        lessons_learned=["Retry pressure compounds failures"],
        **changes,
    ).sealed()


def batch_calls(provider, prefix):
    return [
        body["json"]
        for method, path, body in provider.client.calls
        if method == "POST" and path.startswith(prefix) and path.endswith(":batchUpdate")
    ]


def test_native_operations_require_approval_before_any_api_call(native):
    provider, service = native
    with pytest.raises(ApprovalRequired):
        service.journal(task())
    with pytest.raises(ApprovalRequired):
        service.dashboard([])
    assert not provider.client.calls


def test_google_journal_uses_shared_word_content_revision_guard_and_utf16(native):
    provider, service = native
    entry = task()
    result = service.journal(entry, approved=True)
    assert result["task_id"] == str(entry.id)
    assert result["url"].endswith(f"/{result['id']}/edit")
    doc = provider.client.documents[result["id"]]
    assert "DEMO-1" in doc["text"] and "Backpressure" in doc["text"]
    assert "Retry pressure compounds failures" in doc["text"]
    batch = batch_calls(provider, DOCS)[0]
    assert batch["writeControl"] == {"requiredRevisionId": "rev-1"}
    marker = batch["requests"][-1]["createNamedRange"]["range"]
    assert marker["endIndex"] - marker["startIndex"] == len(doc["text"].encode("utf-16-le")) // 2
    assert any("updateParagraphStyle" in item for item in batch["requests"])
    assert len(provider.client.files) == 1
    assert service.journal(entry, approved=True)["updated"] is False
    assert len(batch_calls(provider, DOCS)) == 1


def test_journal_updates_append_and_preserve_user_text_and_reference_linking(native):
    provider, service = native
    entry = task()
    initial = service.journal(entry, approved=True)
    linked = entry.model_copy(
        update={"references": [initial["url"], f"google-doc-id:{initial['id']}"]}
    ).sealed()
    assert service.journal(linked, approved=True)["updated"] is False
    provider.client.documents[initial["id"]]["text"] += "User notes remain here"
    changed = linked.model_copy(update={"description": "A new verified lesson"}).sealed()
    service.journal(changed, approved=True)
    assert "User notes remain here" in provider.client.documents[initial["id"]]["text"]
    assert "A new verified lesson" in provider.client.documents[initial["id"]]["text"]
    requests = batch_calls(provider, DOCS)[-1]["requests"]
    assert not any("deleteContentRange" in request for request in requests)
    assert len(provider.client.files) == 1


@pytest.mark.parametrize(
    "failure", ["fail_before_batch", "fail_after_batch", "fail_create_after_commit"]
)
def test_journal_reconciles_partial_and_ambiguous_publication(native, failure):
    provider, service = native
    entry = task()
    setattr(provider.client, failure, True)
    with pytest.raises(StorageError):
        service.journal(entry, approved=True)
    result = service.journal(entry, approved=True)
    assert len(provider.client.files) == 1
    assert provider.client.documents[result["id"]]["text"].count("Backpressure") == 1


def test_native_duplicate_task_documents_require_reconciliation(native):
    provider, service = native
    entry = task()
    first = service.journal(entry, approved=True)
    duplicate = copy.deepcopy(provider.client.files[first["id"]])
    duplicate["id"] = "duplicate-id"
    provider.client.files["duplicate-id"] = duplicate
    before = len(provider.client.calls)
    with pytest.raises(ConflictError):
        service.journal(entry, approved=True)
    assert len(provider.client.calls) == before


def test_unknown_native_create_does_not_blindly_repeat(native, monkeypatch):
    provider, service = native
    original = provider.client.request
    calls = []

    def failed(method, path, **kwargs):
        if method == "POST" and path == DRIVE:
            calls.append(path)
            raise StorageError("unknown outcome")
        return original(method, path, **kwargs)

    monkeypatch.setattr(provider.client, "request", failed)
    entry = task()
    with pytest.raises(StorageError):
        service.journal(entry, approved=True)
    with pytest.raises(StorageError, match="unknown outcome"):
        service.journal(entry, approved=True)
    assert len(calls) == 1


def test_native_sheets_reuses_excel_model_and_guards_formula_injection(native):
    provider, service = native
    entry = Record(
        kind="task",
        title='=HYPERLINK("https://evil.invalid","click")',
        status="completed",
        technologies=["Kafka"],
    ).sealed()
    result = service.dashboard([entry], approved=True)
    assert len(result["sheet_ids"]) == 9
    batch = batch_calls(provider, SHEETS)[0]["requests"]
    titles = [r["addSheet"]["properties"]["title"] for r in batch if "addSheet" in r]
    assert titles == [
        "Overview",
        "Tasks",
        "Projects",
        "Decisions",
        "Debugging",
        "Technical Debt",
        "Learning Progress",
        "Review History",
        "Technology Trends",
    ]
    cell_values = [
        cell["userEnteredValue"]
        for request in batch
        if "updateCells" in request
        for row in request["updateCells"]["rows"]
        for cell in row["values"]
    ]
    assert {"stringValue": entry.title} in cell_values
    formulas = [v["formulaValue"] for v in cell_values if "formulaValue" in v]
    assert formulas and all("EKMTasks[" not in f for f in formulas)
    assert any("'Tasks'!D2:D2" in f for f in formulas)
    assert any("setBasicFilter" in r for r in batch)
    assert any("addChart" in r for r in batch)
    assert any("repeatCell" in r for r in batch)
    assert service.dashboard([entry], result["id"], approved=True)["updated"] is False
    assert len(batch_calls(provider, SHEETS)) == 1


def test_sheet_refresh_only_writes_fresh_sheet_ids_and_preserves_custom_tabs(native):
    provider, service = native
    entry = task()
    first = service.dashboard([entry], approved=True)
    spreadsheet = provider.client.spreadsheets[first["id"]]
    spreadsheet["sheets"].append({"properties": {"sheetId": 99, "title": "Personal notes"}})
    snapshot = copy.deepcopy(spreadsheet["sheets"])
    changed = entry.model_copy(update={"description": "Updated"}).sealed()
    second = service.dashboard([changed], first["id"], approved=True)
    assert set(first["sheet_ids"]).isdisjoint(second["sheet_ids"])
    assert spreadsheet["sheets"][: len(snapshot)] == snapshot
    batch = batch_calls(provider, SHEETS)[-1]["requests"]
    for request in batch:
        assert "deleteSheet" not in request
        if "updateCells" in request:
            assert request["updateCells"]["start"]["sheetId"] in second["sheet_ids"]
    assert any("[" in r["addSheet"]["properties"]["title"] for r in batch if "addSheet" in r)


def test_sheet_lost_batch_response_reconciles_snapshot_without_duplicate_tabs(native):
    provider, service = native
    entry = task()
    provider.client.fail_after_batch = True
    with pytest.raises(StorageError):
        service.dashboard([entry], approved=True)
    result = service.dashboard([entry], approved=True)
    assert result["updated"] is False
    assert len(provider.client.spreadsheets[result["id"]]["sheets"]) == 9


def test_pinned_spreadsheet_requires_owned_format_and_parent(native):
    provider, service = native
    initial = service.dashboard([], approved=True)
    provider.client.files[initial["id"]]["parents"] = ["unrelated-folder"]
    before = len(batch_calls(provider, SHEETS))
    with pytest.raises(StorageError):
        service.dashboard([], initial["id"], approved=True)
    assert len(batch_calls(provider, SHEETS)) == before
