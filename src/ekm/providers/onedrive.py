"""Personal OneDrive application-folder records, independently recoverable JSON."""

import json
from pathlib import Path
from urllib.parse import quote
from uuid import UUID

from ekm.microsoft.graph_client import GraphClient
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
        if len(matches) != 1 or "folder" not in matches[0]:
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
            if item.get("name") == name and "folder" in item
        ]
        if len(matches) == 1:
            return matches[0]
        raise


class OneDriveProvider(StorageProvider):
    def __init__(self, graph: GraphClient, root_id: str | None = None):
        self.graph = graph
        self.root_id = root_id

    def _root(self) -> str:
        if not self.root_id:
            raise StorageError("OneDrive is not initialized; approve storage init first.")
        return f"/me/drive/items/{quote(self.root_id, safe='')}"

    def _location(self, kind: Kind, id: UUID) -> str:
        return f"{self._root()}:/database/{FOLDERS[Kind(kind)]}/{UUID(str(id))}.json"

    def initialize_storage(self, *, approved: bool = False) -> None:
        require_approval(approved)
        # Microsoft may create approot on GET. This endpoint is deliberately approval-gated.
        self.root_id = self.graph.request("GET", "/me/drive/special/approot").json()["id"]
        database = ensure_folder(self.graph, self._root(), "database")
        for name in FOLDERS.values():
            ensure_folder(self.graph, f"/me/drive/items/{quote(database['id'], safe='')}", name)
        for name in ("journals", "reports", "dashboards", "manifests"):
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
        metadata = self.graph.request("GET", self._location(kind, id)).json()
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
        if not etag or etag == "*":
            raise ConflictError("An exact version is required for deletion.")
        self.graph.request("DELETE", self._location(kind, id), headers={"If-Match": etag})

    def list_records(self, kind: Kind | None = None) -> list[StoredRecord]:
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
        require_approval(approved)
        folder = "dashboards" if path.suffix.lower() == ".xlsx" else "journals"
        metadata = self.graph.request("GET", f"{self._root()}:/{folder}").json()
        item = self.graph.upload_new(
            f"/me/drive/items/{quote(metadata['id'], safe='')}", path.name, path.read_bytes()
        )
        return item.get("webUrl", item["id"])

    def check_connection(self) -> dict:
        self.graph.request("GET", "/me/drive")
        return {"provider": "onedrive", "connected": True, "initialized": bool(self.root_id)}
