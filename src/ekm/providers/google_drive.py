"""Google Drive storage using immutable, mergeable revision DAGs.

Drive v3's version counter is never treated as an atomic write precondition.
Every change creates a new file; simultaneous changes remain explicit conflicts.
"""

import hashlib
import json
import os
import re
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

DRIVE = "https://www.googleapis.com/drive/v3"
FOLDER_MIME = "application/vnd.google-apps.folder"
FIELDS = "id,name,mimeType,parents,appProperties,version,trashed"
KINDS = {
    Kind.task: "tasks",
    Kind.decision: "decisions",
    Kind.question: "questions",
    Kind.debugging: "debugging",
    Kind.debt: "technical-debt",
    Kind.project: "projects",
    Kind.review: "reviews",
}
ROLES = {"database", "journals", "dashboards", "backups", "manifests", "reports", *KINDS.values()}


def _id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", value):
        raise StorageError("Invalid Google Drive file identifier.")
    return value


def _json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


class GoogleDriveProvider(StorageProvider):
    def __init__(
        self, client, root_id: str | None = None, *, app_id: str, account_id: str, state_dir: Path
    ):
        if not app_id or not account_id:
            raise StorageError("Google storage requires a bound application and account.")
        self.client = client
        self.root_id = _id(root_id) if root_id else None
        self.owner = hashlib.sha256((app_id + "\0" + account_id).encode()).hexdigest()
        self.state_dir = Path(state_dir)

    def owner_properties(self, role: str, **extra) -> dict:
        result = {"ekmOwner": self.owner, "ekmRole": role}
        if set(extra) & set(result):
            raise StorageError("Cannot replace Google ownership properties.")
        result.update(extra)
        if len(result) > 30 or any(
            not isinstance(value, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,30}", key)
            or len((key + value).encode()) > 124
            for key, value in result.items()
        ):
            raise StorageError("Google custom properties exceed their safe size or type limits.")
        return result

    def _root_parent(self) -> str:
        # drive.file can create/list app files under this documented alias even
        # when the user's My Drive root metadata itself is outside the grant.
        return "root"

    def verify_file(self, id: str, role: str, parent_id: str | None = None) -> dict:
        item = self.client.file_metadata(_id(id))
        props = item.get("appProperties", {})
        if (
            item.get("id") != id
            or item.get("trashed")
            or item.get("mimeType") == "application/vnd.google-apps.shortcut"
            or "shortcutDetails" in item
            or not isinstance(props, dict)
            or props.get("ekmOwner") != self.owner
            or props.get("ekmRole") != role
        ):
            raise StorageError(
                "Google file ownership, type or identity does not match this profile."
            )
        if parent_id == "root":
            # Google returns the real parent ID, not the root alias. Verify
            # membership with a scoped query rather than reading root metadata
            # or trusting an arbitrary returned parent ID.
            query = "trashed = false and 'root' in parents"
            for key, value in self.owner_properties(role).items():
                escaped = value.replace("\\", "\\\\").replace("'", "\\'")
                query += f" and appProperties has {{ key='{key}' and value='{escaped}' }}"
            if not any(
                entry.get("id") == id for entry in self.client.iter_files(query, fields=FIELDS)
            ):
                raise StorageError("Google file has moved outside its approved EKM folder.")
        elif parent_id is not None and item.get("parents") != [_id(parent_id)]:
            raise StorageError("Google file has moved outside its approved EKM folder.")
        return item

    def _verify_root(self) -> None:
        if not self.root_id:
            raise StorageError("Google storage is not initialized; approve storage init first.")
        root = self.verify_file(self.root_id, "root", self._root_parent())
        if root.get("mimeType") != FOLDER_MIME:
            raise StorageError("Google EKM root is not an ordinary folder.")

    def _find(self, role: str, parent: str, **extra) -> list[dict]:
        properties = self.owner_properties(role, **extra)
        clauses = ["trashed = false", f"'{_id(parent)}' in parents"]
        for key, value in properties.items():
            # Values are app-generated hashes, UUIDs or roles; escape even those at this boundary.
            escaped = value.replace("\\", "\\\\").replace("'", "\\'")
            clauses.append(f"appProperties has {{ key='{key}' and value='{escaped}' }}")
        items = self.client.iter_files(" and ".join(clauses), fields=FIELDS)
        return [self.verify_file(item["id"], role, parent) for item in items]

    def _mapped_create(
        self,
        operation: str,
        metadata: dict,
        content: bytes | None = None,
        mime_type: str = "application/json",
    ) -> dict:
        """Persist a preallocated binary/folder ID before the potentially ambiguous POST."""
        if metadata["mimeType"].startswith("application/vnd.google-apps.") and metadata[
            "mimeType"
        ] not in {FOLDER_MIME, "application/vnd.google-apps.drive-sdk"}:
            raise StorageError("Native Google Workspace files cannot use preallocated Drive IDs.")
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.state_dir / "google-drive-operations.json"
        lock = FileLock(str(self.state_dir / ".google-drive-operations.lock"))
        with lock:
            try:
                operations = json.loads(path.read_text()) if path.exists() else {}
                if not isinstance(operations, dict):
                    raise ValueError
            except (ValueError, OSError) as error:
                raise StorageError(
                    "Google operation journal is invalid; repair it before retrying."
                ) from error
            if operation not in operations:
                generated = self.client.request(
                    "GET",
                    DRIVE + "/files/generateIds",
                    params={"count": 1, "space": "drive", "type": "files"},
                ).json()
                operations[operation] = _id(generated["ids"][0])
                descriptor, temporary = tempfile.mkstemp(
                    dir=self.state_dir, prefix=".google-operations-"
                )
                try:
                    with os.fdopen(descriptor, "w") as stream:
                        json.dump(operations, stream, sort_keys=True)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, path)
                finally:
                    Path(temporary).unlink(missing_ok=True)
            id = _id(operations[operation])
            body = {**metadata, "id": id}
            try:
                if content is None:
                    return self.client.request(
                        "POST",
                        DRIVE + "/files",
                        json=body,
                        params={"fields": "id,name,mimeType,parents,appProperties,version,trashed"},
                    ).json()
                return self.client.upload_file(body, content, mime_type)
            except ConflictError:
                item = self.client.file_metadata(id)
                parents_match = item.get("parents") == metadata["parents"]
                if metadata["parents"] == ["root"]:
                    self.verify_file(id, metadata["appProperties"]["ekmRole"], "root")
                    parents_match = True
                if (
                    item.get("appProperties") != metadata["appProperties"]
                    or not parents_match
                    or item.get("mimeType") != metadata["mimeType"]
                    or item.get("trashed")
                ):
                    raise ConflictError(
                        "Preallocated Google file ID has incompatible content or ownership."
                    ) from None
                if content is not None and self.client.download(id) != content:
                    raise ConflictError(
                        "Preallocated Google file content differs; no file was replaced."
                    ) from None
                return item

    def initialize_storage(self, *, approved: bool = False) -> None:
        require_approval(approved)
        parent = self._root_parent()
        if self.root_id:
            self._verify_root()
        else:
            matches = self._find("root", parent)
            if len(matches) > 1:
                raise ConflictError("Multiple app-owned Google roots exist; select one explicitly.")
            if matches:
                self.root_id = matches[0]["id"]
            else:
                item = self._mapped_create(
                    "root:" + self.owner,
                    {
                        "name": "EngineeringKnowledge",
                        "mimeType": FOLDER_MIME,
                        "parents": [parent],
                        "appProperties": self.owner_properties("root"),
                    },
                )
                self.root_id = item["id"]
                if len(self._find("root", parent)) != 1:
                    raise ConflictError(
                        "Concurrent Google roots were created; select a root explicitly before continuing."
                    )
            self._verify_root()
        for role in sorted(ROLES):
            self.ensure_folder(role, approved=True)

    def ensure_folder(self, role: str, *, approved: bool = False) -> str:
        if role not in ROLES:
            raise StorageError("Unknown Google EKM folder role.")
        self._verify_root()
        parent = (
            self.ensure_folder("database", approved=approved)
            if role in KINDS.values()
            else self.root_id
        )
        matches = self._find(role, parent)
        if len(matches) > 1:
            raise ConflictError(
                "Multiple app-owned Google folders have the same role; reconcile explicitly."
            )
        if not matches:
            require_approval(approved)
            self._mapped_create(
                "folder:" + self.root_id + ":" + role,
                {
                    "name": role,
                    "mimeType": FOLDER_MIME,
                    "parents": [parent],
                    "appProperties": self.owner_properties(role),
                },
            )
            matches = self._find(role, parent)
            if len(matches) != 1:
                raise ConflictError(
                    "Concurrent Google folder creation requires explicit reconciliation."
                )
        if matches[0].get("mimeType") != FOLDER_MIME:
            raise StorageError("Google EKM folder role refers to a non-folder.")
        return _id(matches[0]["id"])

    @staticmethod
    def _token(record: Record, parents: list[str], deleted: bool) -> str:
        return hashlib.sha256(
            _json(
                {
                    "kind": record.kind.value,
                    "id": str(record.id),
                    "content_hash": record.sealed().content_hash,
                    "parents": sorted(parents),
                    "deleted": deleted,
                }
            )
        ).hexdigest()

    def _nodes(self, kind: Kind, id: UUID) -> dict[str, dict]:
        parent = self.ensure_folder(KINDS[Kind(kind)])
        nodes = {}
        for item in self._find("record", parent, ekmKind=kind.value, ekmId=str(UUID(str(id)))):
            try:
                data = json.loads(self.client.download(item["id"]))
                if (
                    set(data) != {"ekm_revision", "record", "parents", "deleted", "revision"}
                    or data["ekm_revision"] != 1
                    or not isinstance(data["deleted"], bool)
                    or not isinstance(data["parents"], list)
                    or len(set(data["parents"])) != len(data["parents"])
                    or any(
                        not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{64}", token)
                        for token in data["parents"]
                    )
                ):
                    raise ValueError
                record = verify_integrity(Record.model_validate(data["record"]))
                token = self._token(record, data["parents"], data["deleted"])
                if (
                    record.id != UUID(str(id))
                    or record.kind != kind
                    or not record.content_hash
                    or token != data["revision"]
                    or item["appProperties"].get("ekmRevision") != token
                ):
                    raise ValueError
                candidate = {"data": data, "record": record, "file_id": item["id"]}
                if token in nodes:
                    previous = nodes[token]
                    excluded = {"id", "created_at", "updated_at", "content_hash"}
                    if (
                        previous["record"].model_dump(mode="json", exclude=excluded)
                        != record.model_dump(mode="json", exclude=excluded)
                        or sorted(previous["data"]["parents"]) != sorted(data["parents"])
                        or previous["data"]["deleted"] != data["deleted"]
                    ):
                        raise ValueError
                    # Independent writers can serialize the same logical revision
                    # with distinct creation/update timestamps. Choose metadata
                    # deterministically, never a different semantic branch.
                    candidate = min(
                        (previous, candidate),
                        key=lambda node: (
                            node["record"].created_at,
                            node["record"].updated_at,
                            node["file_id"],
                        ),
                    )
                nodes[token] = candidate
            except (ValueError, TypeError, KeyError) as error:
                raise StorageError(
                    "Google immutable record revision is invalid; no history was overwritten."
                ) from error
        for token, node in nodes.items():
            if any(parent not in nodes or parent == token for parent in node["data"]["parents"]):
                raise StorageError(
                    "Google revision history has missing parents; restore or repair it before continuing."
                )
        return nodes

    @staticmethod
    def _heads(nodes: dict[str, dict]) -> list[str]:
        referenced = {parent for node in nodes.values() for parent in node["data"]["parents"]}
        heads = sorted(set(nodes) - referenced)
        if nodes and not heads:
            raise StorageError("Google revision history contains a cycle.")
        return heads

    def _current(self, kind: Kind, id: UUID) -> tuple[dict[str, dict], list[str]]:
        nodes = self._nodes(kind, id)
        return nodes, self._heads(nodes)

    def _append(self, record: Record, parents: list[str], deleted: bool) -> None:
        record = verify_integrity(record).sealed()
        folder = self.ensure_folder(KINDS[record.kind])
        token = self._token(record, parents, deleted)
        data = {
            "ekm_revision": 1,
            "record": record.model_dump(mode="json"),
            "parents": sorted(parents),
            "deleted": deleted,
            "revision": token,
        }
        self._mapped_create(
            "revision:" + self.root_id + ":" + token,
            {
                "name": f"{record.id}-{token}.json",
                "mimeType": "application/json",
                "parents": [folder],
                "appProperties": self.owner_properties(
                    "record", ekmKind=record.kind.value, ekmId=str(record.id), ekmRevision=token
                ),
            },
            _json(data),
        )

    def create_record(self, record: Record, *, approved: bool = False) -> StoredRecord:
        require_approval(approved)
        nodes, heads = self._current(record.kind, record.id)
        if len(heads) > 1:
            raise ConflictError("Google record has divergent revision heads; resolve explicitly.")
        if heads:
            current = nodes[heads[0]]
            if (
                not current["data"]["deleted"]
                and current["record"].sealed().content_hash == record.sealed().content_hash
            ):
                return StoredRecord(current["record"], heads[0])
            raise ConflictError(
                "Google record already exists, including retained deletion history."
            )
        self._append(record, [], False)
        return self.get_record(record.kind, record.id)

    def get_record(self, kind: Kind, id: UUID) -> StoredRecord:
        nodes, heads = self._current(kind, id)
        if len(heads) > 1:
            raise ConflictError(
                "Google record has divergent revision heads; no branch was selected."
            )
        if not heads or nodes[heads[0]]["data"]["deleted"]:
            raise NotFoundError("Google record is absent or explicitly tombstoned.")
        return StoredRecord(nodes[heads[0]]["record"], heads[0])

    def update_record(self, record: Record, etag: str, *, approved: bool = False) -> StoredRecord:
        require_approval(approved)
        current = self.get_record(record.kind, record.id)
        if current.etag != etag:
            raise ConflictError("Google logical revision changed; refresh and merge explicitly.")
        self._append(record, [etag], False)
        return self.get_record(record.kind, record.id)

    def delete_record(self, kind: Kind, id: UUID, etag: str, *, approved: bool = False) -> None:
        require_approval(approved)
        current = self.get_record(kind, id)
        if current.etag != etag:
            raise ConflictError("Google logical revision changed; deletion was not appended.")
        self._append(current.record, [etag], True)
        nodes, heads = self._current(kind, id)
        if len(heads) != 1 or not nodes[heads[0]]["data"]["deleted"]:
            raise ConflictError(
                "Concurrent Google revisions were retained; resolve deletion explicitly."
            )

    def _identities(self, kind: Kind) -> list[UUID]:
        parent = self.ensure_folder(KINDS[kind])
        values = self._find("record", parent, ekmKind=kind.value)
        try:
            return sorted({UUID(item["appProperties"]["ekmId"]) for item in values}, key=str)
        except (ValueError, KeyError, TypeError) as error:
            raise StorageError("Google record metadata contains an invalid identity.") from error

    def list_records(self, kind: Kind | None = None) -> list[StoredRecord]:
        result = []
        for selected in [Kind(kind)] if kind is not None else Kind:
            for id in self._identities(selected):
                try:
                    result.append(self.get_record(selected, id))
                except NotFoundError:
                    pass
        return result

    def list_record_metadata(self) -> list[dict]:
        return [
            {
                "id": str(item.record.id),
                "kind": item.record.kind.value,
                "etag": item.etag,
                "content_hash": item.record.content_hash,
            }
            for item in self.list_records()
        ]

    def conflicts(self) -> list[dict]:
        result = []
        for kind in Kind:
            for id in self._identities(kind):
                nodes, heads = self._current(kind, id)
                if len(heads) > 1:
                    result.append(
                        {
                            "kind": kind.value,
                            "id": str(id),
                            "heads": heads,
                            "branches": [
                                {
                                    "etag": head,
                                    "deleted": nodes[head]["data"]["deleted"],
                                    "record": nodes[head]["record"].model_dump(mode="json"),
                                }
                                for head in heads
                            ],
                        }
                    )
        return result

    def resolve_conflict(
        self,
        kind: Kind,
        id: UUID,
        heads: list[str],
        record: Record | None,
        *,
        approved: bool = False,
    ) -> StoredRecord | None:
        require_approval(approved)
        nodes, current = self._current(kind, id)
        if len(current) < 2 or sorted(heads) != current:
            raise ConflictError(
                "Google conflict heads changed; inspect all current branches before merging."
            )
        if record is not None and (record.kind != kind or record.id != UUID(str(id))):
            raise StorageError("Merge record identity differs from the conflicted record.")
        chosen = record or nodes[current[0]]["record"]
        self._append(chosen, current, record is None)
        nodes, after = self._current(kind, id)
        if len(after) != 1:
            raise ConflictError(
                "A concurrent revision remains; inspect the preserved branches again."
            )
        return None if record is None else self.get_record(kind, id)

    def upload_document(self, path: Path, *, approved: bool = False) -> str:
        require_approval(approved)
        path = Path(path)
        if path.name in {"", ".", ".."} or any(ord(char) < 32 for char in path.name):
            raise StorageError("Use a plain document filename.")
        role = {".docx": "journals", ".xlsx": "dashboards", ".json": "backups"}.get(
            path.suffix.lower()
        )
        if role is None:
            raise StorageError("Upload a DOCX, XLSX or JSON artifact.")
        parent = self.ensure_folder(role)
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        name_hash = hashlib.sha256(path.name.encode()).hexdigest()
        existing = self._find("artifact", parent, ekmName=name_hash)
        if len(existing) > 1:
            raise ConflictError("Duplicate Google artifact names require explicit reconciliation.")
        if existing:
            if self.client.download(existing[0]["id"]) != content:
                raise ConflictError(
                    "Google artifact name already exists with different content; create a new snapshot."
                )
            return f"https://drive.google.com/file/d/{existing[0]['id']}/view"
        mime = {
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ".json": "application/json",
        }[path.suffix.lower()]
        item = self._mapped_create(
            "artifact:" + parent + ":" + name_hash + ":" + digest,
            {
                "name": path.name,
                "mimeType": mime,
                "parents": [parent],
                "appProperties": self.owner_properties(
                    "artifact", ekmName=name_hash, ekmHash=digest
                ),
            },
            content,
            mime,
        )
        if len(self._find("artifact", parent, ekmName=name_hash)) != 1:
            raise ConflictError(
                "Concurrent Google artifact creation produced multiple snapshots; all files were retained."
            )
        return f"https://drive.google.com/file/d/{item['id']}/view"

    def check_connection(self) -> dict:
        response = self.client.request(
            "GET", DRIVE + "/about", params={"fields": "user(displayName,emailAddress)"}
        ).json()
        if self.root_id:
            self._verify_root()
        return {
            "provider": "google",
            "connected": True,
            "initialized": bool(self.root_id),
            "account": response.get("user", {}).get("emailAddress"),
        }
