"""Storage contract shared by all account types."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from ekm.models import Kind, Record


class StorageError(RuntimeError):
    """A safe-to-display storage failure."""


class ConflictError(StorageError):
    """A record changed since it was read; the caller must reconcile."""


class NotFoundError(StorageError):
    """The requested remote record does not exist."""


class ApprovalRequired(StorageError):
    """A mutation was requested without explicit approval."""


def require_approval(approved: bool) -> None:
    if not approved:
        raise ApprovalRequired(
            "Preview the destination and payload, then explicitly approve this write."
        )


@dataclass(frozen=True)
class StoredRecord:
    record: Record
    etag: str


class StorageProvider(ABC):
    @abstractmethod
    def initialize_storage(self, *, approved: bool = False) -> None: ...

    @abstractmethod
    def create_record(self, record: Record, *, approved: bool = False) -> StoredRecord: ...

    @abstractmethod
    def get_record(self, kind: Kind, id: UUID) -> StoredRecord: ...

    @abstractmethod
    def update_record(
        self, record: Record, etag: str, *, approved: bool = False
    ) -> StoredRecord: ...

    @abstractmethod
    def delete_record(self, kind: Kind, id: UUID, etag: str, *, approved: bool = False) -> None: ...

    @abstractmethod
    def list_records(self, kind: Kind | None = None) -> list[StoredRecord]: ...

    @abstractmethod
    def upload_document(self, path: Path, *, approved: bool = False) -> str: ...

    @abstractmethod
    def check_connection(self) -> dict: ...

    def export_data(self) -> list[dict]:
        return [item.record.model_dump(mode="json") for item in self.list_records()]

    def search_records(self, query: str) -> list[StoredRecord]:
        import json

        terms = query.casefold().split()
        return [
            item
            for item in self.list_records()
            if all(
                term in json.dumps(item.record.model_dump(mode="json")).casefold() for term in terms
            )
        ]

    def get_sync_status(self) -> dict:
        return {"provider": type(self).__name__, "remote_records": len(self.list_records())}
