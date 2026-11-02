"""Durable local remote simulator, usable without Microsoft credentials."""

import hashlib
import json
import os
import tempfile
from pathlib import Path
from uuid import UUID

from filelock import FileLock

from ekm.models import Kind, Record, verify_integrity
from ekm.providers.base import (
    ConflictError,
    NotFoundError,
    StorageError,
    StorageProvider,
    StoredRecord,
    require_approval,
)


class MockStorageProvider(StorageProvider):
    def __init__(self, root: Path):
        self.root = Path(root)

    def _lock(self) -> FileLock:
        if not self.root.is_dir():
            raise StorageError("Storage is not initialized; run storage init with approval.")
        return FileLock(str(self.root / ".write.lock"))

    def initialize_storage(self, *, approved: bool = False) -> None:
        require_approval(approved)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        for kind in Kind:
            (self.root / kind.value).mkdir(exist_ok=True, mode=0o700)
        (self.root / "documents").mkdir(exist_ok=True, mode=0o700)

    def _path(self, kind: Kind, id: UUID) -> Path:
        return self.root / Kind(kind).value / f"{UUID(str(id))}.json"

    @staticmethod
    def _encode(record: Record) -> bytes:
        return json.dumps(
            record.model_dump(mode="json"), sort_keys=True, ensure_ascii=False
        ).encode()

    @staticmethod
    def _etag(data: bytes) -> str:
        return '"' + hashlib.sha256(data).hexdigest() + '"'

    def _atomic(self, path: Path, data: bytes) -> None:
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".pending-")
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def create_record(self, record: Record, *, approved: bool = False) -> StoredRecord:
        require_approval(approved)
        with self._lock():
            path = self._path(record.kind, record.id)
            data = self._encode(record)
            if path.exists():
                existing = self.get_record(record.kind, record.id)
                if existing.record.model_dump(mode="json") == record.model_dump(mode="json"):
                    return existing
                raise ConflictError("Record already exists; read and reconcile before updating.")
            self._atomic(path, data)
            return StoredRecord(record, self._etag(data))

    def get_record(self, kind: Kind, id: UUID) -> StoredRecord:
        try:
            data = self._path(kind, id).read_bytes()
        except FileNotFoundError as error:
            raise NotFoundError("Record not found.") from error
        try:
            record = verify_integrity(Record.model_validate(json.loads(data)))
        except (ValueError, TypeError) as error:
            raise StorageError(
                "Stored record is invalid; restore or repair it before continuing."
            ) from error
        if record.kind != kind or record.id != UUID(str(id)):
            raise StorageError("Stored record identity does not match its location.")
        return StoredRecord(record, self._etag(data))

    def update_record(self, record: Record, etag: str, *, approved: bool = False) -> StoredRecord:
        require_approval(approved)
        with self._lock():
            existing = self.get_record(record.kind, record.id)
            if not etag or existing.etag != etag:
                raise ConflictError(
                    "Version conflict; fetch the current record and merge explicitly."
                )
            data = self._encode(record)
            self._atomic(self._path(record.kind, record.id), data)
            return StoredRecord(record, self._etag(data))

    def delete_record(self, kind: Kind, id: UUID, etag: str, *, approved: bool = False) -> None:
        require_approval(approved)
        with self._lock():
            if self.get_record(kind, id).etag != etag:
                raise ConflictError("Version conflict; deletion was not performed.")
            self._path(kind, id).unlink()

    def list_records(self, kind: Kind | None = None) -> list[StoredRecord]:
        if not self.root.is_dir():
            raise StorageError("Storage is not initialized.")
        records = []
        for selected in [kind] if kind is not None else Kind:
            for path in sorted((self.root / selected.value).glob("*.json")):
                try:
                    id = UUID(path.stem)
                except ValueError as error:
                    raise StorageError("Invalid record filename in remote storage.") from error
                records.append(self.get_record(selected, id))
        return records

    def upload_document(self, path: Path, *, approved: bool = False) -> str:
        require_approval(approved)
        with self._lock():
            target = self.root / "documents" / path.name
            if target.exists():
                raise ConflictError("Document exists; choose a new export filename.")
            self._atomic(target, path.read_bytes())
            return str(target)

    def check_connection(self) -> dict:
        return {"provider": "mock", "connected": self.root.is_dir()}
