"""Durable, snapshot-bound download queue and explicit conflict reconciliation."""

import hashlib
import json
from uuid import UUID

from ekm.models import Kind, Record, utcnow, verify_integrity
from ekm.providers.base import ConflictError, NotFoundError, StorageError, StoredRecord


class Synchronizer:
    def __init__(self, engine):
        self.engine = engine
        self.db = engine.db
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS downloads (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, etag TEXT NOT NULL,
                expected_hash TEXT, payload TEXT, state TEXT NOT NULL, error TEXT);
            CREATE TABLE IF NOT EXISTS resolution_backups (
                sequence INTEGER PRIMARY KEY, id TEXT NOT NULL, payload TEXT NOT NULL,
                etag TEXT, resolved_at TEXT NOT NULL);
        """)

    def status(self) -> dict:
        pending = self.engine.pending()
        return {
            "profile": self.engine.profile.name,
            "upload_queue": [
                {"id": p["record"]["id"], "kind": p["record"]["kind"], "operation": p["operation"]}
                for p in pending
            ],
            "download_queue": [
                dict(zip(("id", "kind", "etag", "state", "error"), row))
                for row in self.db.execute("SELECT id,kind,etag,state,error FROM downloads")
            ],
            "metadata": dict(self.db.execute("SELECT key,value FROM metadata")),
            "remote_checked": False,
        }

    def pull(self) -> dict:
        provider = self.engine.provider
        with self.engine.lock:
            if not hasattr(provider, "list_record_metadata"):
                return {
                    "downloaded": self.engine.refresh(),
                    "conflicts": [],
                    "failed": [],
                    "queued": 0,
                }
            # All pages and kinds must enumerate before the cache or queue is changed.
            snapshot = provider.list_record_metadata()
            normalized = []
            seen = set()
            for entry in snapshot:
                record_id = str(UUID(str(entry["id"])))
                kind = Kind(entry["kind"]).value
                etag = entry["etag"]
                if record_id in seen or not etag or etag == "*":
                    raise StorageError("Remote snapshot contains duplicate IDs or missing versions")
                seen.add(record_id)
                normalized.append(
                    {
                        "id": record_id,
                        "kind": kind,
                        "etag": etag,
                        "content_hash": entry.get("content_hash"),
                    }
                )
            signature = hashlib.sha256(
                json.dumps(sorted(normalized, key=lambda r: r["id"]), sort_keys=True).encode()
            ).hexdigest()
            with self.db:
                self.db.execute(
                    "CREATE TEMP TABLE IF NOT EXISTS snapshot_ids (id TEXT PRIMARY KEY)"
                )
                self.db.execute("DELETE FROM snapshot_ids")
                for entry in normalized:
                    self.db.execute("INSERT INTO snapshot_ids VALUES (?)", (entry["id"],))
                    old = self.db.execute(
                        "SELECT kind,etag,expected_hash FROM downloads WHERE id=?", (entry["id"],)
                    ).fetchone()
                    if old != (entry["kind"], entry["etag"], entry["content_hash"]):
                        self.db.execute(
                            "INSERT OR REPLACE INTO downloads VALUES (?,?,?,?,NULL,'pending',NULL)",
                            (entry["id"], entry["kind"], entry["etag"], entry["content_hash"]),
                        )
                self.db.execute(
                    "DELETE FROM downloads WHERE id NOT IN (SELECT id FROM snapshot_ids)"
                )
                self.db.execute(
                    "INSERT OR REPLACE INTO metadata VALUES ('pull_snapshot',?)", (signature,)
                )
            conflicts, failed = [], []
            for record_id, kind, etag, expected_hash, payload, state in self.db.execute(
                "SELECT id,kind,etag,expected_hash,payload,state FROM downloads"
            ).fetchall():
                if state == "ready" and payload:
                    try:
                        cached = verify_integrity(Record.model_validate_json(payload))
                        if str(cached.id) != record_id or cached.kind.value != kind:
                            raise ValueError("staged record identity mismatch")
                        if expected_hash and cached.content_hash != expected_hash:
                            raise ValueError("staged record hash mismatch")
                        continue
                    except ValueError:
                        with self.db:
                            self.db.execute(
                                "UPDATE downloads SET payload=NULL,state='pending',error='staged_payload_invalid' WHERE id=?",
                                (record_id,),
                            )
                try:
                    stored = provider.get_record(Kind(kind), UUID(record_id))
                    verify_integrity(stored.record)
                    if str(stored.record.id) != record_id or stored.record.kind.value != kind:
                        raise StorageError("remote record identity does not match download queue")
                    if stored.etag != etag or (
                        expected_hash and stored.record.content_hash != expected_hash
                    ):
                        raise ConflictError("remote snapshot changed during download")
                    with self.db:
                        self.db.execute(
                            "UPDATE downloads SET payload=?,state='ready',error=NULL WHERE id=?",
                            (stored.record.model_dump_json(), record_id),
                        )
                except (ConflictError, NotFoundError):
                    conflicts.append(record_id)
                    with self.db:
                        self.db.execute(
                            "UPDATE downloads SET state='pending',error='remote_changed' WHERE id=?",
                            (record_id,),
                        )
                except (StorageError, ValueError):
                    failed.append(record_id)
                    with self.db:
                        self.db.execute(
                            "UPDATE downloads SET state='pending',error='download_failed' WHERE id=?",
                            (record_id,),
                        )
            if conflicts or failed:
                return {
                    "downloaded": 0,
                    "conflicts": conflicts,
                    "failed": failed,
                    "queued": len(normalized),
                }
            # Publish the validated snapshot atomically. Preserve prior committed cache rows
            # with pending local work, even when the corresponding remote record was removed.
            with self.db:
                protected = {
                    r[0]
                    for r in self.db.execute(
                        "SELECT id FROM pending UNION SELECT id FROM pending_deletes"
                    )
                }
                rows = self.db.execute("SELECT id FROM records").fetchall()
                for (record_id,) in rows:
                    if record_id not in protected:
                        self.db.execute("DELETE FROM records WHERE id=?", (record_id,))
                        self.db.execute("DELETE FROM search WHERE id=?", (record_id,))
                for payload, etag in self.db.execute(
                    "SELECT payload,etag FROM downloads"
                ).fetchall():
                    self.engine._cache_rows(StoredRecord(Record.model_validate_json(payload), etag))
                self.db.execute("DELETE FROM downloads")
                self.db.execute(
                    "INSERT OR REPLACE INTO metadata VALUES ('last_pull',?)",
                    (utcnow().isoformat(),),
                )
            return {"downloaded": len(normalized), "conflicts": [], "failed": [], "queued": 0}

    def resolve(
        self,
        kind: Kind,
        record_id: UUID,
        *,
        strategy: str,
        merged: dict | None,
        expected_etag: str | None,
        approved: bool,
        dry_run: bool,
    ) -> dict:
        if strategy not in {"remote", "merge"}:
            raise ValueError("conflict strategy must be remote or merge")
        if not approved and not dry_run:
            raise ValueError("conflict resolution requires explicit approval")
        with self.engine.lock:
            row = self.db.execute(
                "SELECT payload,etag FROM pending WHERE id=?", (str(record_id),)
            ).fetchone()
            if not row:
                raise ValueError("no pending upload for this record; inspect sync status")
            pending = Record.model_validate_json(row[0])
            if pending.kind != kind:
                raise ValueError("pending record kind does not match")
            try:
                remote = self.engine.provider.get_record(kind, record_id)
            except NotFoundError:
                remote = None
            remote_etag = remote.etag if remote else None
            if not dry_run and expected_etag != remote_etag:
                raise ConflictError(
                    "provide the current remote ETag shown by sync resolve --dry-run"
                )
            record = self.engine.prepare(merged) if merged is not None else None
            if strategy == "merge" and (
                record is None or record.id != record_id or record.kind != kind
            ):
                raise ValueError("merge requires a validated record with the same UUID and kind")
            preview = {
                "profile": self.engine.profile.name,
                "strategy": strategy,
                "pending": pending.model_dump(mode="json"),
                "remote_etag": remote_etag,
                "remote": remote.record.model_dump(mode="json") if remote else None,
                "merged": record.model_dump(mode="json") if record else None,
            }
            if dry_run:
                return preview
            with self.db:
                self.db.execute(
                    "INSERT INTO resolution_backups(id,payload,etag,resolved_at) VALUES (?,?,?,?)",
                    (str(record_id), row[0], row[1], utcnow().isoformat()),
                )
                self.db.execute("DELETE FROM pending WHERE id=?", (str(record_id),))
                if strategy == "merge":
                    self.db.execute(
                        "INSERT INTO pending VALUES (?,?,?)",
                        (str(record_id), record.model_dump_json(), remote_etag),
                    )
                elif remote:
                    self.engine._cache_rows(remote)
                else:
                    self.db.execute("DELETE FROM records WHERE id=?", (str(record_id),))
                    self.db.execute("DELETE FROM search WHERE id=?", (str(record_id),))
            self.engine.audit("resolve", str(record_id), strategy)
            if strategy == "merge":
                self.engine._send(record, remote_etag)
            return {
                "resolved": str(record_id),
                "strategy": strategy,
                "local_draft_retained": "resolution_backups",
            }
