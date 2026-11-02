"""Company SharePoint Lists with stable unique identity and ETag concurrency."""

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
from ekm.providers.onedrive import ensure_folder

LISTS = {
    Kind("task"): "EKM_Tasks",
    Kind("decision"): "EKM_Decisions",
    Kind("question"): "EKM_Questions",
    Kind("debugging"): "EKM_Debugging",
    Kind("debt"): "EKM_TechnicalDebt",
    Kind("project"): "EKM_Projects",
    Kind("review"): "EKM_ReviewHistory",
}


class SharePointProvider(StorageProvider):
    def __init__(self, graph: GraphClient, site_id: str, drive_id: str | None = None):
        if not site_id:
            raise StorageError("A company SharePoint site ID is required.")
        self.graph = graph
        self.site_id = site_id
        self.drive_id = drive_id
        self._list_ids: dict[Kind, str] = {}

    @property
    def _site(self) -> str:
        return f"/sites/{quote(self.site_id, safe=',')}"

    @staticmethod
    def _columns() -> list[dict]:
        return [
            {"name": "EKMId", "text": {}, "indexed": True, "enforceUniqueValues": True},
            {"name": "EKMPayload", "text": {"allowMultipleLines": True, "textType": "plain"}},
            {"name": "EKMProject", "text": {}, "indexed": True},
            {"name": "EKMHash", "text": {}},
        ]

    def _list(self, kind: Kind) -> str:
        if kind not in self._list_ids:
            matches = [
                item
                for item in self.graph.iter_collection(f"{self._site}/lists")
                if item.get("displayName") == LISTS[kind]
            ]
            if len(matches) != 1:
                raise StorageError(
                    "Knowledge list missing or ambiguous; approve storage init or repair the site."
                )
            self._list_ids[kind] = matches[0]["id"]
        return f"{self._site}/lists/{quote(self._list_ids[kind], safe='')}"

    def initialize_storage(self, *, approved: bool = False) -> None:
        require_approval(approved)
        lists = self.graph.iter_collection(f"{self._site}/lists")
        for kind, name in LISTS.items():
            matches = [item for item in lists if item.get("displayName") == name]
            if len(matches) > 1:
                raise StorageError("Duplicate EKM list names; resolve before provisioning.")
            if not matches:
                result = self.graph.request(
                    "POST",
                    f"{self._site}/lists",
                    json={
                        "displayName": name,
                        "list": {"template": "genericList"},
                        "columns": self._columns(),
                    },
                ).json()
                self._list_ids[kind] = result["id"]
            else:
                self._list_ids[kind] = matches[0]["id"]
            columns = self.graph.iter_collection(f"{self._list(kind)}/columns")
            by_name = {column["name"]: column for column in columns}
            if not all(column["name"] in by_name for column in self._columns()):
                raise StorageError(
                    "Existing EKM list schema is incompatible; no existing list was overwritten."
                )
            identity = by_name["EKMId"]
            payload = by_name["EKMPayload"].get("text", {})
            if (
                not identity.get("enforceUniqueValues")
                or not identity.get("indexed")
                or not payload.get("allowMultipleLines")
            ):
                raise StorageError(
                    "Existing list lacks unique indexed IDs or multiline payload; repair its schema."
                )
        self._document_folder()

    @staticmethod
    def _fields(record: Record) -> dict:
        payload = json.dumps(record.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
        if len(payload.encode("utf-16-le")) // 2 > 60000:
            raise StorageError("Record exceeds the SharePoint 60,000-character application limit.")
        return {
            "Title": record.title[:255],
            "EKMId": str(record.id),
            "EKMPayload": payload,
            "EKMProject": record.project_id or "",
            "EKMHash": record.content_hash,
        }

    @staticmethod
    def _stored(item: dict, kind: Kind, id: UUID | None = None) -> StoredRecord:
        try:
            record = verify_integrity(
                Record.model_validate(json.loads(item["fields"]["EKMPayload"]))
            )
        except (KeyError, ValueError, TypeError) as error:
            raise StorageError(
                "SharePoint record payload is invalid; repair it before continuing."
            ) from error
        if (
            record.kind != kind
            or (id is not None and record.id != UUID(str(id)))
            or str(record.id) != item["fields"].get("EKMId")
        ):
            raise StorageError("SharePoint record identity does not match its fields.")
        etag = item.get("eTag") or item.get("@odata.etag")
        if not etag:
            raise StorageError("SharePoint omitted the item version.")
        return StoredRecord(record, etag)

    def _item(self, kind: Kind, id: UUID) -> dict:
        selected = UUID(str(id))
        path = f"{self._list(kind)}/items?$expand=fields&$filter=fields/EKMId%20eq%20'{selected}'"
        values = self.graph.iter_collection(path)
        if not values:
            raise NotFoundError("SharePoint record not found.")
        if len(values) != 1:
            raise ConflictError("Duplicate remote stable IDs; reconcile before changing records.")
        return values[0]

    def create_record(self, record: Record, *, approved: bool = False) -> StoredRecord:
        require_approval(approved)
        try:
            existing = self.get_record(record.kind, record.id)
        except NotFoundError:
            existing = None
        if existing:
            if existing.record.model_dump(mode="json") == record.model_dump(mode="json"):
                return existing
            raise ConflictError("Record already exists; use an explicit version-aware update.")
        try:
            self.graph.request(
                "POST", f"{self._list(record.kind)}/items", json={"fields": self._fields(record)}
            )
        except ConflictError:
            existing = self.get_record(record.kind, record.id)
            if existing.record.model_dump(mode="json") == record.model_dump(mode="json"):
                return existing
            raise
        return self.get_record(record.kind, record.id)

    def get_record(self, kind: Kind, id: UUID) -> StoredRecord:
        return self._stored(self._item(kind, id), kind, id)

    def update_record(self, record: Record, etag: str, *, approved: bool = False) -> StoredRecord:
        require_approval(approved)
        if not etag or etag == "*":
            raise ConflictError("An exact item version is required for updates.")
        item = self._item(record.kind, record.id)
        self.graph.request(
            "PATCH",
            f"{self._list(record.kind)}/items/{quote(item['id'], safe='')}/fields",
            headers={"If-Match": etag},
            json=self._fields(record),
        )
        return self.get_record(record.kind, record.id)

    def delete_record(self, kind: Kind, id: UUID, etag: str, *, approved: bool = False) -> None:
        require_approval(approved)
        if not etag or etag == "*":
            raise ConflictError("An exact item version is required for deletion.")
        item = self._item(kind, id)
        self.graph.request(
            "DELETE",
            f"{self._list(kind)}/items/{quote(item['id'], safe='')}",
            headers={"If-Match": etag},
        )

    def list_records(self, kind: Kind | None = None) -> list[StoredRecord]:
        return [
            self._stored(item, selected)
            for selected in ([kind] if kind is not None else Kind)
            for item in self.graph.iter_collection(f"{self._list(selected)}/items?$expand=fields")
        ]

    def _document_folder(self) -> tuple[str, dict]:
        if not self.drive_id:
            self.drive_id = self.graph.request("GET", f"{self._site}/drive").json()["id"]
        drive = f"/drives/{quote(self.drive_id, safe='')}"
        return drive, ensure_folder(self.graph, f"{drive}/root", "EngineeringKnowledge")

    def upload_document(self, path: Path, *, approved: bool = False) -> str:
        require_approval(approved)
        drive, folder = self._document_folder()
        item = self.graph.upload_new(
            f"{drive}/items/{quote(folder['id'], safe='')}", path.name, path.read_bytes()
        )
        return item.get("webUrl", item["id"])

    def check_connection(self) -> dict:
        self.graph.request("GET", self._site)
        return {"provider": "sharepoint", "connected": True, "site_id": self.site_id}
