"""Personal OneDrive application-folder records, independently recoverable JSON."""

import hashlib
import json
import os
import tempfile
from pathlib import Path
from urllib.parse import quote
from uuid import UUID, uuid4

from filelock import FileLock

from ekm.microsoft.graph_client import GraphClient
from ekm.microsoft.onedrive_client import OneDriveClient, safe_name
from ekm.models import Kind, Record, verify_integrity
from ekm.providers.base import (
    ConflictError,
    NotFoundError,
    StorageError,
    StorageProvider,
    StoredRecord,
    require_approval,
)

FOLDERS = {
    Kind("task"): "tasks",
    Kind("decision"): "decisions",
    Kind("question"): "questions",
    Kind("debugging"): "debugging",
    Kind("debt"): "technical-debt",
    Kind("project"): "projects",
    Kind("review"): "reviews",
}


def ensure_folder(graph: GraphClient, parent_path: str, name: str) -> dict:
    """Only call from an already approved mutation."""
    matches = [
        item
        for item in graph.iter_collection(f"{parent_path}/children")
        if item.get("name") == name
    ]
    if matches:
        if len(matches) != 1 or "folder" not in matches[0] or "remoteItem" in matches[0]:
            raise StorageError("Storage folder collides with an existing non-folder item.")
        return matches[0]
    try:
        return graph.request(
            "POST",
            f"{parent_path}/children",
            json={"name": name, "folder": {}, "@microsoft.graph.conflictBehavior": "fail"},
        ).json()
    except ConflictError:
        matches = [
            item
            for item in graph.iter_collection(f"{parent_path}/children")
            if item.get("name") == name and "folder" in item and "remoteItem" not in item
        ]
        if len(matches) == 1:
            return matches[0]
        raise


class OneDriveProvider(StorageProvider):
    def __init__(
        self,
        graph: GraphClient,
        root_id: str | None = None,
        *,
        location: str = "approot",
        app_id: str | None = None,
        account_id: str | None = None,
        state_dir: Path | None = None,
    ):
        if location not in {"approot", "drive-root"}:
            raise StorageError("Unsupported OneDrive storage location.")
        if location == "drive-root" and (not app_id or not account_id):
            raise StorageError(
                "Root-folder storage requires a bound application and account identity."
            )
        self.graph = graph
        self.root_id = root_id
        self.location = location
        self.app_id = app_id
        self.account_id = account_id
        self.state_dir = Path(state_dir) if state_dir is not None else None

    def _owner(self) -> dict:
        return {
            "schema_version": 1,
            "owner": "ekm",
            "app_id": self.app_id,
            "account_hash": hashlib.sha256(str(self.account_id).encode()).hexdigest(),
            "root_id": self.root_id,
        }

    def _verify_ownership(self) -> None:
        if self.location == "approot":
            return
        path = f"{self._root()}:/_ekm-owner.json"
        try:
            metadata = self.graph.request("GET", path).json()
            content = self.graph.download(
                f"/me/drive/items/{quote(metadata['id'], safe='')}/content"
            )
            after = self.graph.request("GET", path).json()
            if metadata.get("eTag") != after.get("eTag") or json.loads(content) != self._owner():
                raise StorageError("OneDrive ownership marker does not match this bound profile.")
        except (NotFoundError, ValueError, KeyError) as error:
            raise StorageError(
                "OneDrive ownership marker is missing or invalid; initialize with explicit adoption only if this is your intended folder."
            ) from error

    def _verify_record_scope(self, kind: Kind | None = None) -> None:
        if self.location == "approot":
            return
        self._verify_ownership()
        paths = [f"{self._root()}:/database"]
        paths.extend(
            f"{self._root()}:/database/{FOLDERS[selected]}"
            for selected in ([kind] if kind is not None else Kind)
        )
        for path in paths:
            item = self.graph.request("GET", path).json()
            if "folder" not in item or "remoteItem" in item:
                raise StorageError(
                    "Knowledge records must remain in ordinary EKM folders; shortcuts are not permitted."
                )

    def _root(self) -> str:
        if not self.root_id:
            raise StorageError("OneDrive is not initialized; approve storage init first.")
        return f"/me/drive/items/{quote(self.root_id, safe='')}"

    def _location(self, kind: Kind, id: UUID) -> str:
        return f"{self._root()}:/database/{FOLDERS[Kind(kind)]}/{UUID(str(id))}.json"

    def initialize_storage(self, *, approved: bool = False, adopt_existing: bool = False) -> None:
        require_approval(approved)
        if self.location == "approot":
            # Microsoft may create approot on GET. This endpoint is approval-gated.
            self.root_id = self.graph.request("GET", "/me/drive/special/approot").json()["id"]
        else:
            matches = [
                item
                for item in self.graph.iter_collection("/me/drive/root/children")
                if item.get("name", "").casefold() == "engineeringknowledge"
            ]
            created = False
            if len(matches) > 1 or (
                matches and ("folder" not in matches[0] or "remoteItem" in matches[0])
            ):
                raise StorageError("EngineeringKnowledge collides with an existing item.")
            if matches:
                selected = matches[0]
            else:
                selected = self.graph.request(
                    "POST",
                    "/me/drive/root/children",
                    json={
                        "name": "EngineeringKnowledge",
                        "folder": {},
                        "@microsoft.graph.conflictBehavior": "fail",
                    },
                ).json()
                created = True
            if self.root_id and self.root_id != selected["id"]:
                raise StorageError(
                    "Configured root ID differs from EngineeringKnowledge; no destination was changed."
                )
            self.root_id = selected["id"]
            try:
                self.graph.request("GET", f"{self._root()}:/_ekm-owner.json")
            except NotFoundError:
                if not created and not adopt_existing:
                    raise StorageError(
                        "Existing EngineeringKnowledge has no ownership marker. Explicit --adopt-existing is required, including recovery after interrupted initialization."
                    )
                self.graph.upload_new(
                    self._root(),
                    "_ekm-owner.json",
                    json.dumps(self._owner(), sort_keys=True).encode(),
                )
            self._verify_ownership()
        database = ensure_folder(self.graph, self._root(), "database")
        for name in FOLDERS.values():
            ensure_folder(self.graph, f"/me/drive/items/{quote(database['id'], safe='')}", name)
        for name in ("journals", "reports", "dashboards", "manifests", "backups"):
            ensure_folder(self.graph, self._root(), name)

    @staticmethod
    def _payload(record: Record) -> bytes:
        data = json.dumps(
            record.model_dump(mode="json"), sort_keys=True, ensure_ascii=False
        ).encode()
        if len(data) > 1024 * 1024:
            raise StorageError("Record exceeds the 1 MiB application record limit.")
        return data

    @staticmethod
    def _stored(record: Record, metadata: dict) -> StoredRecord:
        etag = metadata.get("eTag") or metadata.get("@odata.etag")
        if not etag:
            raise StorageError(
                "Microsoft omitted the version; reconcile remote state before retrying."
            )
        return StoredRecord(record, etag)

    def create_record(self, record: Record, *, approved: bool = False) -> StoredRecord:
        require_approval(approved)
        self._verify_record_scope(record.kind)
        try:
            existing = self.get_record(record.kind, record.id)
        except NotFoundError:
            existing = None
        if existing is not None:
            if existing.record.model_dump(mode="json") == record.model_dump(mode="json"):
                return existing
            raise ConflictError("Record already exists; read and reconcile before updating.")
        folder = self.graph.request(
            "GET", f"{self._root()}:/database/{FOLDERS[record.kind]}"
        ).json()
        try:
            metadata = self.graph.upload_new(
                f"/me/drive/items/{quote(folder['id'], safe='')}",
                f"{record.id}.json",
                self._payload(record),
            )
        except ConflictError:
            existing = self.get_record(record.kind, record.id)
            if existing.record.model_dump(mode="json") == record.model_dump(mode="json"):
                return existing
            raise
        return self._stored(record, metadata)

    def get_record(self, kind: Kind, id: UUID) -> StoredRecord:
        self._verify_record_scope(kind)
        metadata = self.graph.request("GET", self._location(kind, id)).json()
        if "remoteItem" in metadata:
            raise StorageError("Knowledge record shortcuts are not permitted.")
        data = self.graph.download(f"/me/drive/items/{quote(metadata['id'], safe='')}/content")
        after = self.graph.request("GET", self._location(kind, id)).json()
        if metadata.get("eTag") != after.get("eTag"):
            raise ConflictError(
                "Record changed during download; fetch it again before using its version."
            )
        try:
            record = verify_integrity(Record.model_validate(json.loads(data)))
        except (ValueError, TypeError) as error:
            raise StorageError(
                "Remote JSON record is invalid; repair it before continuing."
            ) from error
        if record.kind != kind or record.id != UUID(str(id)):
            raise StorageError("Remote record identity does not match its location.")
        return self._stored(record, metadata)

    def update_record(self, record: Record, etag: str, *, approved: bool = False) -> StoredRecord:
        require_approval(approved)
        self._verify_record_scope(record.kind)
        if not etag or etag == "*":
            raise ConflictError("An exact version is required for updates.")
        metadata = self.graph.request(
            "PUT",
            f"{self._location(record.kind, record.id)}:/content",
            headers={"If-Match": etag, "Content-Type": "application/json"},
            content=self._payload(record),
        ).json()
        return self._stored(record, metadata)

    def delete_record(self, kind: Kind, id: UUID, etag: str, *, approved: bool = False) -> None:
        require_approval(approved)
        self._verify_record_scope(kind)
        if not etag or etag == "*":
            raise ConflictError("An exact version is required for deletion.")
        self.graph.request("DELETE", self._location(kind, id), headers={"If-Match": etag})

    def list_records(self, kind: Kind | None = None) -> list[StoredRecord]:
        self._verify_record_scope(kind)
        records = []
        for selected in [kind] if kind is not None else Kind:
            path = f"{self._root()}:/database/{FOLDERS[selected]}:/children"
            for item in self.graph.iter_collection(path):
                name = item.get("name", "")
                if not name.endswith(".json") or "file" not in item:
                    continue
                try:
                    id = UUID(name[:-5])
                except ValueError as error:
                    raise StorageError("Remote record filename is invalid.") from error
                records.append(self.get_record(selected, id))
        return records

    def upload_document(self, path: Path, *, approved: bool = False) -> str:
        item = self.upload_file(path, approved=approved)
        return item["url"] or item["id"]

    def _client(self) -> OneDriveClient:
        self._root()
        return OneDriveClient(self.graph, self.root_id)

    def list_record_metadata(self) -> list[dict]:
        self._verify_record_scope()
        records = []
        for kind in Kind:
            for item in self.graph.iter_collection(
                f"{self._root()}:/database/{FOLDERS[kind]}:/children"
            ):
                name = item.get("name", "")
                if "file" not in item or not name.endswith(".json"):
                    continue
                if "remoteItem" in item:
                    raise StorageError("Knowledge record shortcuts are not permitted.")
                try:
                    id = str(UUID(name[:-5]))
                except ValueError as error:
                    raise StorageError("Remote record filename is invalid.") from error
                etag = item.get("eTag") or item.get("@odata.etag")
                if not etag:
                    raise StorageError("Remote listing omitted a record ETag.")
                records.append({"id": id, "kind": kind.value, "etag": etag, "content_hash": None})
        return records

    def _ledger_key(self, folder: str, name: str) -> str:
        return f"{self.root_id}/{folder}/{safe_name(name)}"

    def _ledger(self) -> tuple[Path, FileLock]:
        if self.state_dir is None:
            raise StorageError("Document updates require a profile-specific provenance directory.")
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        return self.state_dir / "documents.json", FileLock(str(self.state_dir / ".documents.lock"))

    @staticmethod
    def _read_ledger(path: Path) -> dict:
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text())
            if not isinstance(data, dict):
                raise ValueError
            if any(
                not isinstance(entry, dict)
                or any(
                    not isinstance(entry.get(field), str) or not entry[field]
                    for field in ("id", "etag", "content_hash")
                )
                for entry in data.values()
            ):
                raise ValueError
            return data
        except (ValueError, OSError) as error:
            raise StorageError(
                "Document provenance is invalid; use a new snapshot filename."
            ) from error

    def _remember(self, folder: str, name: str, metadata: dict) -> None:
        if self.state_dir is None:
            return
        path, lock = self._ledger()
        with lock:
            data = self._read_ledger(path)
            data[self._ledger_key(folder, name)] = metadata
            fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".documents-")
            try:
                with os.fdopen(fd, "w") as stream:
                    json.dump(data, stream, sort_keys=True)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            finally:
                Path(temporary).unlink(missing_ok=True)

    @staticmethod
    def _document_folder(path: Path, folder: str | None = None) -> str:
        if folder is not None:
            return folder
        selected = {".docx": "journals", ".xlsx": "dashboards", ".json": "backups"}.get(
            path.suffix.lower()
        )
        if selected is None:
            raise StorageError("Upload a DOCX journal, XLSX dashboard, or JSON backup.")
        return selected

    def upload_file(self, path: Path, folder: str | None = None, *, approved: bool = False) -> dict:
        require_approval(approved)
        self._verify_ownership()
        path = Path(path)
        folder = self._document_folder(path, folder)
        client = self._client()
        content = path.read_bytes()
        try:
            result = client.upload(folder, path.name, content, approved=True)
        except ConflictError:
            existing, result = client.download(folder, path.name)
            if existing != content:
                raise ConflictError(
                    "Existing document differs; choose a new filename or explicitly update an EKM-owned version."
                ) from None
            # Exact-byte replay is safe. Do not claim provenance of an unrelated existing file.
            return result
        self._remember(folder, path.name, result)
        return result

    def update_document(self, path: Path, etag: str, *, approved: bool = False) -> dict:
        require_approval(approved)
        self._verify_ownership()
        path = Path(path)
        folder = self._document_folder(path)
        ledger_path, lock = self._ledger()
        with lock:
            previous = self._read_ledger(ledger_path).get(self._ledger_key(folder, path.name))
            if not previous:
                raise StorageError(
                    "This document has no EKM creation provenance; export a new snapshot."
                )
            old_content, current = self._client().download(folder, path.name)
            if (
                not etag
                or etag == "*"
                or current["etag"] != etag
                or previous["etag"] != etag
                or previous["id"] != current["id"]
                or previous["content_hash"] != hashlib.sha256(old_content).hexdigest()
            ):
                raise ConflictError(
                    "Remote document has changed since EKM created it; preserve user edits and export a new snapshot."
                )
            result = self._client().update(
                folder, path.name, path.read_bytes(), etag, approved=True
            )
        self._remember(folder, path.name, result)
        return result

    def download_document(self, folder: str, name: str, path: Path) -> dict:
        self._verify_ownership()
        return self._client().download_to(folder, name, path)

    def file_metadata(self, folder: str, name: str) -> dict:
        self._verify_ownership()
        return self._client().metadata(folder, name)

    def document_metadata(self, folder: str, name: str) -> dict:
        return self.file_metadata(folder, name)

    def list_files(self, folder: str) -> list[dict]:
        self._verify_ownership()
        return self._client().list(folder)

    def backup_data(self, records: list[dict], name: str, *, approved: bool = False) -> dict:
        require_approval(approved)
        self._verify_ownership()
        validated = [
            verify_integrity(Record.model_validate(record)).model_dump(mode="json")
            for record in records
        ]
        payload = json.dumps({"schema_version": 1, "records": validated}, sort_keys=True).encode()
        return self._client().upload("backups", safe_name(name), payload, approved=True)

    def test_connection(self, *, approved: bool = False) -> dict:
        require_approval(approved)
        self._verify_ownership()
        identity = self.graph.request("GET", "/me").json()
        self.graph.request("GET", "/me/drive")
        self._client().list("manifests")
        name = f"ekm-test-{uuid4()}.json"
        payload = json.dumps({"ekm_test": str(uuid4())}, sort_keys=True).encode()
        created = None
        cleanup = "not-created"
        try:
            created = self._client().upload("manifests", name, payload, approved=True)
            content, current = self._client().download("manifests", name)
            if (
                content != payload
                or current["id"] != created["id"]
                or current["etag"] != created["etag"]
            ):
                raise StorageError(
                    "Temporary OneDrive test content changed; cleanup will not touch unrelated versions."
                )
        finally:
            if created is None:
                # A response can be lost after a successful upload; reconcile this unguessable name only.
                try:
                    content, current = self._client().download("manifests", name)
                    if content == payload:
                        created = current
                except NotFoundError:
                    pass
            if created is not None:
                self._verify_ownership()
                self._client().delete(
                    "manifests", name, created["etag"], approved=True, expected_id=created["id"]
                )
                cleanup = "deleted"
        return {
            "connected": True,
            "account": identity.get("userPrincipalName") or identity.get("displayName", ""),
            "temporary_file": name,
            "cleanup": cleanup,
        }

    def check_connection(self) -> dict:
        self.graph.request("GET", "/me/drive")
        return {"provider": "onedrive", "connected": True, "initialized": bool(self.root_id)}
