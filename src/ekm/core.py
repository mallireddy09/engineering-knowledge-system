"""Shared knowledge workflows, durable pending writes, and a rebuildable SQLite index."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import Counter
from datetime import datetime, timedelta
from uuid import NAMESPACE_URL, UUID, uuid5

from filelock import FileLock

from ekm.config import Config, Profile
from ekm.models import Kind, Record, migrate_record, utcnow
from ekm.providers.base import ConflictError, NotFoundError, StorageError, StoredRecord
from ekm.security import redact


def make_provider(config: Config, profile: Profile):
    from ekm.providers.mock import MockStorageProvider

    if profile.account_type == "mock":
        return MockStorageProvider(config.profile_dir(profile) / "remote")
    if profile.account_type == "google":
        from pathlib import Path

        from ekm.google.auth import GoogleAuth
        from ekm.google.client import GoogleClient
        from ekm.providers.google_drive import GoogleDriveProvider

        auth = GoogleAuth(
            config.profile_dir(profile),
            Path(profile.google_credentials_file),
            env_file=Path(profile.google_env_file) if profile.google_env_file else None,
        )
        identity = auth.bound_identity()
        if not identity or identity["client_id"] != profile.client_id:
            raise StorageError(
                "sign in to the configured Google profile using ekm auth google login"
            )
        client = GoogleClient(auth.access_token)
        return GoogleDriveProvider(
            client,
            root_id=profile.root_id,
            app_id=profile.client_id,
            account_id=identity["sub"],
            state_dir=config.profile_dir(profile),
        )
    from ekm.microsoft.auth import MicrosoftAuth
    from ekm.microsoft.graph_client import GraphClient
    from ekm.providers.onedrive import OneDriveProvider
    from ekm.providers.sharepoint import SharePointProvider

    auth = MicrosoftAuth(
        config.profile_dir(profile),
        profile.client_id,
        profile.account_type,
        profile.tenant_id or None,
        scopes=profile.scopes,
        redirect_uri=profile.redirect_uri,
    )
    graph = GraphClient(auth.access_token)
    if profile.account_type == "personal":
        binding = auth.bound_identity()
        return OneDriveProvider(
            graph,
            root_id=profile.root_id,
            location=profile.storage_location,
            app_id=profile.client_id,
            account_id=binding.get("home_account_id") if binding else None,
            state_dir=config.profile_dir(profile),
        )
    return SharePointProvider(graph, profile.site_id, drive_id=profile.drive_id)


class Engine:
    def __init__(self, config: Config, profile: Profile, provider=None):
        self.config, self.profile = config, profile
        self.directory = config.profile_dir(profile)
        self._provider = provider
        self.lock = FileLock(str(self.directory / "engine.lock"))
        self.db = sqlite3.connect(self.directory / "cache.sqlite")
        (self.directory / "cache.sqlite").chmod(0o600)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS records (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL,
                etag TEXT NOT NULL, content_hash TEXT NOT NULL);
            CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(id UNINDEXED, text);
            CREATE TABLE IF NOT EXISTS pending (
                id TEXT PRIMARY KEY, payload TEXT NOT NULL, etag TEXT);
            CREATE TABLE IF NOT EXISTS pending_deletes (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, etag TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tombstones (id TEXT PRIMARY KEY, deleted_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)

    def close(self) -> None:
        self.db.close()
        graph = getattr(self._provider, "graph", None)
        if graph is not None:
            graph.close()
        client = getattr(self._provider, "client", None)
        if client is not None and client is not graph:
            client.close()

    @property
    def provider(self):
        if self._provider is None:
            self._provider = make_provider(self.config, self.profile)
        return self._provider

    def _cache_rows(self, stored: StoredRecord) -> None:
        record = stored.record
        self.db.execute(
            "INSERT OR REPLACE INTO records VALUES (?,?,?,?,?)",
            (
                str(record.id),
                record.kind.value,
                record.model_dump_json(),
                stored.etag,
                record.content_hash,
            ),
        )
        self.db.execute("DELETE FROM search WHERE id=?", (str(record.id),))
        self.db.execute("INSERT INTO search VALUES (?,?)", (str(record.id), self._text(record)))

    def _cache(self, stored: StoredRecord) -> None:
        with self.db:
            self._cache_rows(stored)

    def _prune_cache(self) -> None:
        """Remove committed rows without pending work inside the caller's transaction."""
        protected = "SELECT id FROM pending UNION SELECT id FROM pending_deletes"
        self.db.execute(f"DELETE FROM records WHERE id NOT IN ({protected})")
        self.db.execute(f"DELETE FROM search WHERE id NOT IN ({protected})")

    @staticmethod
    def _text(record: Record) -> str:
        data = record.model_dump(mode="json", exclude_defaults=True)
        excluded = {"id", "schema_version", "content_hash", "created_at", "updated_at"}
        values = [value for key, value in data.items() if key not in excluded]
        return " ".join(
            " ".join(str(x) for x in value) if isinstance(value, list) else str(value)
            for value in values
        ).casefold()

    def audit(self, operation: str, record_id: str, result: str) -> None:
        # No payloads, prompts, tokens, or Graph error bodies enter audit logs.
        path = self.directory / "audit.jsonl"
        with path.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {
                        "at": utcnow().isoformat(),
                        "operation": operation,
                        "record_id": record_id,
                        "result": result,
                    }
                )
                + "\n"
            )
        path.chmod(0o600)

    def prepare(self, data: dict) -> Record:
        data = redact(data)
        record = migrate_record(data)
        if "id" not in data:
            # Stable logical IDs make retries and simultaneous creates converge.
            identity = (
                record.task_id
                if record.kind == Kind.task and record.task_id
                else record.title.casefold()
            )
            key = json.dumps(
                [
                    self.profile.name,
                    record.kind.value,
                    record.project_id,
                    record.repository,
                    identity,
                ]
            )
            record = record.model_copy(update={"id": uuid5(NAMESPACE_URL, key)})
        return record.sealed()

    def preview(self, record: Record, *, etag: str | None = None) -> dict:
        record = self.prepare(record.model_dump(mode="json"))
        body = {
            "profile": self.profile.model_dump(),
            "operation": "update" if etag else "create",
            "etag": etag,
            "record": record.model_dump(mode="json"),
        }
        body["approval_hash"] = hashlib.sha256(
            json.dumps(body, sort_keys=True).encode()
        ).hexdigest()
        return body

    def save(
        self,
        record: Record,
        *,
        approved: bool = False,
        etag: str | None = None,
        offline: bool = False,
        expected_hash: str | None = None,
    ) -> StoredRecord | None:
        if not approved:
            raise ValueError("saving requires approval of the redacted record and destination")
        record = self.prepare(record.model_dump(mode="json"))
        preview = self.preview(record, etag=etag)
        if expected_hash and preview["approval_hash"] != expected_hash:
            raise ValueError("approval hash changed; preview and approve again")
        with self.lock:
            if self.db.execute("SELECT 1 FROM tombstones WHERE id=?", (str(record.id),)).fetchone():
                raise ConflictError(
                    "record was deleted locally; explicitly restore its tombstone before saving"
                )
            existing = self.db.execute(
                "SELECT payload, etag FROM pending WHERE id=?", (str(record.id),)
            ).fetchone()
            if existing and (
                Record.model_validate_json(existing[0]).content_hash != record.content_hash
                or existing[1] != etag
            ):
                raise ConflictError(
                    "a different pending version exists; resolve or discard it first"
                )
            with self.db:
                self.db.execute(
                    "INSERT OR REPLACE INTO pending VALUES (?,?,?)",
                    (str(record.id), record.model_dump_json(), etag),
                )
            if offline:
                self.audit("queue", str(record.id), "pending")
                return None
            return self._send(record, etag)

    def _send(self, record: Record, etag: str | None) -> StoredRecord:
        try:
            try:
                remote = self.provider.get_record(record.kind, record.id)
            except NotFoundError:
                remote = None
            if remote and remote.record.content_hash == record.content_hash:
                stored = remote  # Reconcile a remote success followed by local failure.
            elif remote:
                if etag is None:
                    raise ConflictError(
                        "record exists with different content; use its current ETag to update"
                    )
                stored = self.provider.update_record(record, etag, approved=True)
            elif etag is not None:
                raise ConflictError("record was removed; update cannot recreate it")
            else:
                # Duplicate detection across records with explicitly supplied IDs.
                duplicate = next(
                    (
                        r
                        for r in self.provider.list_records(record.kind)
                        if r.record.content_hash == record.content_hash
                    ),
                    None,
                )
                stored = duplicate or self.provider.create_record(record, approved=True)
            self._cache(stored)
            with self.db:
                self.db.execute("DELETE FROM pending WHERE id=?", (str(record.id),))
            self.audit("save", str(stored.record.id), "saved")
            return stored
        except Exception:
            self.audit("save", str(record.id), "pending-or-conflict")
            raise

    def push(self, *, approved: bool = False) -> dict:
        if not approved:
            raise ValueError("sync requires explicit approval for pending writes")
        results = {"saved": [], "deleted": [], "conflicts": [], "failed": []}
        with self.lock:
            deletions = self.db.execute(
                "SELECT id,kind,etag FROM pending_deletes ORDER BY rowid"
            ).fetchall()
            for record_id, kind, etag in deletions:
                try:
                    self._send_delete(Kind(kind), UUID(record_id), etag)
                    results["deleted"].append(record_id)
                except ConflictError:
                    results["conflicts"].append(record_id)
                except StorageError:
                    results["failed"].append(record_id)
            rows = self.db.execute("SELECT payload, etag FROM pending ORDER BY rowid").fetchall()
            for payload, etag in rows:
                record = Record.model_validate_json(payload)
                try:
                    self._send(record, etag)
                    results["saved"].append(str(record.id))
                except ConflictError:
                    results["conflicts"].append(str(record.id))
                except StorageError:
                    results["failed"].append(str(record.id))
        return results

    def sync(self, *, approved: bool = False) -> dict:
        results = self.push(approved=approved)
        results["pull"] = self.pull()
        return results

    def sync_status(self) -> dict:
        from ekm.sync import Synchronizer

        return Synchronizer(self).status()

    def pull(self) -> dict:
        from ekm.sync import Synchronizer

        return Synchronizer(self).pull()

    def resolve(
        self,
        kind: Kind,
        record_id: UUID,
        *,
        strategy: str,
        merged: dict | None = None,
        expected_etag: str | None = None,
        approved: bool = False,
        dry_run: bool = False,
    ) -> dict:
        from ekm.sync import Synchronizer

        return Synchronizer(self).resolve(
            kind,
            record_id,
            strategy=strategy,
            merged=merged,
            expected_etag=expected_etag,
            approved=approved,
            dry_run=dry_run,
        )

    def refresh(self) -> int:
        # Only replace the index after a full provider listing succeeds.
        with self.lock:
            records = self.provider.list_records()
            with self.db:
                self._prune_cache()
                for stored in records:
                    self._cache_rows(stored)
                self.db.execute(
                    "INSERT OR REPLACE INTO metadata VALUES ('last_refresh', ?)",
                    (utcnow().isoformat(),),
                )
        return len(records)

    def records(
        self,
        *,
        kind: Kind | None = None,
        project: str | None = None,
        technology: str | None = None,
        priority: str | None = None,
    ) -> list[Record]:
        rows = self.db.execute("SELECT payload FROM records ORDER BY id").fetchall()
        records = [Record.model_validate_json(r[0]) for r in rows]
        return [
            r
            for r in records
            if (kind is None or r.kind == kind)
            and (project is None or r.project_id == project or r.name == project)
            and (
                technology is None
                or technology.casefold() in {t.casefold() for t in [*r.technologies, r.technology]}
            )
            and (priority is None or r.priority == priority)
        ]

    def get(self, kind: Kind, record_id: UUID, *, remote: bool = False) -> StoredRecord:
        if remote:
            stored = self.provider.get_record(kind, record_id)
            self._cache(stored)
            return stored
        row = self.db.execute(
            "SELECT payload,etag FROM records WHERE id=? AND kind=?", (str(record_id), kind.value)
        ).fetchone()
        if not row:
            raise NotFoundError("record not in cache; run ekm refresh")
        return StoredRecord(record=Record.model_validate_json(row[0]), etag=row[1])

    def pending(self) -> list[dict]:
        writes = [
            {"operation": "save", "record": json.loads(payload), "etag": etag}
            for payload, etag in self.db.execute("SELECT payload,etag FROM pending")
        ]
        deletes = [
            {"operation": "delete", "record": {"id": record_id, "kind": kind}, "etag": etag}
            for record_id, kind, etag in self.db.execute("SELECT id,kind,etag FROM pending_deletes")
        ]
        return writes + deletes

    def discard(self, record_id: UUID, *, approved: bool = False) -> None:
        if not approved:
            raise ValueError("discarding a pending draft requires approval")
        with self.lock, self.db:
            self.db.execute("DELETE FROM pending WHERE id=?", (str(record_id),))
            self.db.execute("DELETE FROM pending_deletes WHERE id=?", (str(record_id),))
        self.audit("discard", str(record_id), "discarded")

    def delete(self, kind: Kind, record_id: UUID, etag: str, *, approved: bool = False) -> None:
        if not approved:
            raise ValueError("deletion requires explicit approval")
        with self.lock:
            with self.db:
                self.db.execute(
                    "INSERT OR REPLACE INTO pending_deletes VALUES (?,?,?)",
                    (str(record_id), kind.value, etag),
                )
                self.db.execute(
                    "INSERT OR REPLACE INTO tombstones VALUES (?,?)",
                    (str(record_id), utcnow().isoformat()),
                )
                self.db.execute("DELETE FROM pending WHERE id=?", (str(record_id),))
            self._send_delete(kind, record_id, etag)

    def _send_delete(self, kind: Kind, record_id: UUID, etag: str) -> None:
        try:
            self.provider.delete_record(kind, record_id, etag, approved=True)
        except NotFoundError:
            pass  # A previous approved attempt may have succeeded before the local acknowledgement.
        with self.db:
            self.db.execute("DELETE FROM records WHERE id=?", (str(record_id),))
            self.db.execute("DELETE FROM search WHERE id=?", (str(record_id),))
            self.db.execute("DELETE FROM pending_deletes WHERE id=?", (str(record_id),))
        self.audit("delete", str(record_id), "deleted")

    def revive_deleted(self, record_id: UUID, *, approved: bool = False) -> None:
        if not approved:
            raise ValueError("restoring a tombstone requires explicit approval")
        with self.lock, self.db:
            if self.db.execute(
                "SELECT 1 FROM pending_deletes WHERE id=?", (str(record_id),)
            ).fetchone():
                raise ConflictError(
                    "resolve or discard pending deletion before restoring its tombstone"
                )
            self.db.execute("DELETE FROM tombstones WHERE id=?", (str(record_id),))
        self.audit("revive", str(record_id), "tombstone-cleared")

    def report_records(self, *, project: str | None = None) -> list[Record]:
        result = []
        for record in self.records(project=project):
            if record.kind == Kind.question:
                state = self.question_state(record)
                record = record.model_copy(
                    update={
                        "review_count": state["review_count"],
                        "correct_count": state["correct_count"],
                        "confidence_score": state["confidence_score"],
                        "next_review_at": datetime.fromisoformat(state["next_review_at"]),
                    }
                )
            result.append(record)
        return result

    def search(self, query: str, **filters) -> list[dict]:
        tokens = re.findall(r"\w+", query.casefold())
        if not tokens:
            return []
        expression = " OR ".join('"' + t + '"' for t in tokens)
        hits = {
            row[0]
            for row in self.db.execute("SELECT id FROM search WHERE text MATCH ?", (expression,))
        }
        found = []
        for record in self.records(**filters):
            text = self._text(record)
            matched = [t for t in tokens if t in text]
            if str(record.id) in hits or matched:
                found.append(
                    {
                        "record": record.model_dump(mode="json"),
                        "similarity": len(matched) / len(tokens),
                        "explanation": "Matching terms: " + ", ".join(matched),
                        "caution": "Historical evidence requires verification in the current environment",
                    }
                )
        return sorted(found, key=lambda x: x["similarity"], reverse=True)

    def related(self, record: Record) -> list[Record]:
        ids = {*record.related_task_ids, *record.technical_decisions, *record.debugging_references}
        if record.question_id:
            ids.add(record.question_id)
        return [r for r in self.records() if r.id in ids or record.id in r.related_task_ids]

    def question_state(self, question: Record, now: datetime | None = None) -> dict:
        now = now or utcnow()
        reviews = sorted(
            [r for r in self.records(kind=Kind.review) if r.question_id == question.id],
            key=lambda r: (r.review_date or r.created_at, str(r.id)),
        )
        streak = 0
        for review in reviews:
            streak = streak + 1 if review.score >= 0.7 else 0
        if reviews:
            last = reviews[-1]
            interval = question.review_schedule[
                min(max(streak - 1, 0), len(question.review_schedule) - 1)
            ]
            due = (last.review_date or last.created_at) + timedelta(days=interval)
        else:
            due = question.next_review_at or question.created_at + timedelta(
                days=question.review_schedule[0]
            )
        return {
            "question_id": str(question.id),
            "question": question.question,
            "project_id": question.project_id,
            "topic": question.topic,
            "difficulty": question.difficulty,
            "next_review_at": due.isoformat(),
            "due": due <= now,
            "review_count": len(reviews),
            "correct_count": sum(r.score >= 0.7 for r in reviews),
            "confidence_score": (sum(r.score for r in reviews) / len(reviews)) if reviews else 0,
        }

    def questions(
        self,
        *,
        due: bool = False,
        project: str | None = None,
        topic: str | None = None,
        difficulty: str | None = None,
    ) -> list[dict]:
        result = []
        for question in self.records(kind=Kind.question, project=project):
            state = self.question_state(question)
            if (
                (not due or state["due"])
                and (not topic or question.topic == topic)
                and (not difficulty or question.difficulty == difficulty)
            ):
                result.append(state)
        return sorted(result, key=lambda x: x["next_review_at"])

    def review(
        self,
        question_id: UUID,
        answer: str,
        score: float,
        feedback: str,
        *,
        approved: bool = False,
        evaluator: str = "human",
        offline: bool = False,
    ):
        question = self.get(Kind.question, question_id).record
        record = Record(
            kind=Kind.review,
            title=f"Review: {question.title}",
            question_id=question_id,
            user_answer=answer,
            expected_answer=question.expected_answer,
            score=score,
            feedback=feedback,
            review_date=utcnow(),
            project_id=question.project_id,
            evaluator=evaluator,
        )
        return self.save(record.sealed(), approved=approved, offline=offline)

    def insights(self, *, project: str | None = None, since: datetime | None = None) -> dict:
        records = [r for r in self.records(project=project) if not since or r.updated_at >= since]
        tasks = [r for r in records if r.kind == Kind.task]
        reviews = [r for r in records if r.kind == Kind.review]
        debugging = [r for r in records if r.kind == Kind.debugging]
        return {
            "record_count": len(records),
            "kinds": dict(Counter(r.kind.value for r in records)),
            "tasks_by_status": dict(Counter(r.status for r in tasks)),
            "technology_distribution": dict(Counter(t for r in records for t in r.technologies)),
            "project_activity": dict(Counter(r.project_id or "unassigned" for r in records)),
            "recurring_issues": dict(Counter(r.root_cause for r in debugging if r.root_cause)),
            "debt_by_status": dict(Counter(r.status for r in records if r.kind == Kind.debt)),
            "review_count": len(reviews),
            "quiz_average": sum(r.score for r in reviews) / len(reviews) if reviews else None,
            "learning_gaps": [
                s for s in self.questions(project=project) if s["confidence_score"] < 0.7
            ],
            "due_questions": len(self.questions(due=True, project=project)),
            "note": "Statuses are recorded assertions; unknown is never inferred as complete.",
        }
