"""One CLI for both agents. Remote writes always require a reviewed destination."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import timedelta
from pathlib import Path
from uuid import UUID

from pydantic import ValidationError

from ekm import __version__
from ekm.config import Config, Profile
from ekm.core import Engine
from ekm.models import Kind, Record, utcnow, verify_integrity
from ekm.providers.base import StorageError
from ekm.security import redact


def emit(value) -> None:
    print(json.dumps(redact(value), indent=2, default=str))


def confirm(args, message: str) -> None:
    if getattr(args, "approve", False):
        return
    if not sys.stdin.isatty() or input(message + " [yes/no] ").strip().lower() != "yes":
        raise ValueError("approval required; review the preview and rerun with --approve")


def write_flags(parser) -> None:
    parser.add_argument("--input", type=Path, help="validated record JSON file")
    parser.add_argument(
        "--approve", action="store_true", help="explicit approval of preview and destination"
    )
    parser.add_argument("--dry-run", action="store_true", help="preview without remote calls")
    parser.add_argument(
        "--offline", action="store_true", help="queue approved draft without contacting storage"
    )
    parser.add_argument("--etag", help="required current version when updating an existing record")
    parser.add_argument(
        "--expected-hash", help="require the exact approval_hash from a full-record preview"
    )
    parser.add_argument(
        "--preview-output",
        type=Path,
        help="save the fully populated redacted draft for repeatable approval",
    )


def filters(parser) -> None:
    parser.add_argument("--project")
    parser.add_argument("--technology")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="ekm", description=__doc__)
    root.add_argument("--version", action="version", version=__version__)
    root.add_argument("--profile", help="read a named profile; switch explicitly before writing")
    commands = root.add_subparsers(dest="command", required=True)
    account = commands.add_parser("account", help="isolated destination profiles")
    ac = account.add_subparsers(dest="action", required=True)
    add = ac.add_parser("add")
    add.add_argument("name")
    add.add_argument(
        "--type", dest="account_type", choices=["mock", "personal", "company"], required=True
    )
    for option in ("client-id", "tenant-id", "site-id", "drive-id"):
        add.add_argument("--" + option, default=None if option == "drive-id" else "")
    add.add_argument("--scope", action="append", dest="scopes")
    ac.add_parser("list")
    ac.add_parser("status")
    for action in ("switch", "remove"):
        item = ac.add_parser(action)
        item.add_argument("name")
        item.add_argument("--approve", action="store_true")
    ac.add_parser("login")
    ac.add_parser("logout")
    init = commands.add_parser("init", help="explicitly provision the selected remote storage")
    init.add_argument("--approve", action="store_true")
    commands.add_parser("refresh", help="rebuild local cache from a complete remote listing")
    commands.add_parser("check", help="check the configured provider connection")
    sync = commands.add_parser("sync", help="send reviewed pending drafts and refresh cache")
    sync.add_argument("action", nargs="?", choices=["status", "pull", "push", "resolve"])
    sync.add_argument("--approve", action="store_true")
    sync.add_argument("--kind", choices=[k.value for k in Kind])
    sync.add_argument("--id", type=UUID)
    sync.add_argument("--strategy", choices=["remote", "merge"])
    sync.add_argument("--input", type=Path)
    sync.add_argument("--etag")
    sync.add_argument("--dry-run", action="store_true")
    commands.add_parser("pending")
    discard = commands.add_parser("discard", help="discard a local pending draft")
    discard.add_argument("id", type=UUID)
    discard.add_argument("--approve", action="store_true")
    revive = commands.add_parser(
        "revive", help="explicitly allow recreation of a locally deleted record"
    )
    revive.add_argument("id", type=UUID)
    revive.add_argument("--approve", action="store_true")
    log = commands.add_parser(
        "log", help="draft from Git metadata or save agent-extracted knowledge"
    )
    write_flags(log)
    log.add_argument("--task")
    log.add_argument("--project")
    log.add_argument("--repo", type=Path, default=Path.cwd())
    log.add_argument("--title")
    for command in ("import", "update"):
        item = commands.add_parser(command)
        write_flags(item)
    for command in ("decision", "debug", "debt", "project"):
        item = commands.add_parser(command)
        item.add_argument("action", nargs="?", default="list")
        item.add_argument("value", nargs="?")
        write_flags(item)
        filters(item)
        if command == "debt":
            item.add_argument("--priority", choices=["low", "medium", "high", "critical"])
    search = commands.add_parser("search")
    search.add_argument("query")
    search.add_argument("--kind", choices=[k.value for k in Kind])
    filters(search)
    for name in ("show", "delete"):
        item = commands.add_parser(name)
        item.add_argument("kind", choices=[k.value for k in Kind])
        item.add_argument("id", type=UUID)
        if name == "show":
            item.add_argument("--remote", action="store_true")
            item.add_argument("--related", action="store_true")
        else:
            item.add_argument("--etag", required=True)
            item.add_argument("--approve", action="store_true")
    for command in ("review", "quiz"):
        item = commands.add_parser(command)
        item.add_argument("--due", action="store_true")
        item.add_argument("--project")
        item.add_argument("--topic")
        item.add_argument("--difficulty", choices=["beginner", "intermediate", "advanced"])
        item.add_argument("--question", type=UUID)
        item.add_argument("--answer")
        item.add_argument("--score", type=float)
        item.add_argument("--feedback", default="")
        item.add_argument("--evaluator", choices=["human", "agent"], default="human")
        item.add_argument("--approve", action="store_true")
        item.add_argument("--offline", action="store_true")
    for command in ("insights", "weekly", "monthly"):
        item = commands.add_parser(command)
        item.add_argument("--project")
        item.add_argument("--recurring-issues", action="store_true")
        item.add_argument("--learning-gaps", action="store_true")
    for command in ("export", "backup"):
        item = commands.add_parser(command)
        item.add_argument("--format", choices=["json", "word", "excel"], default="json")
        item.add_argument("--output", type=Path, required=True)
        item.add_argument("--project")
        item.add_argument("--upload", action="store_true")
        item.add_argument("--approve", action="store_true")
    restore = commands.add_parser("restore")
    restore.add_argument("--input", type=Path, required=True)
    restore.add_argument("--approve", action="store_true")
    restore.add_argument("--dry-run", action="store_true")
    restore.add_argument("--offline", action="store_true")
    restore.add_argument(
        "--revive-deleted",
        action="store_true",
        help="explicitly clear local deletion protection for restored IDs",
    )
    github = commands.add_parser("github", help="optional read-only issue or PR metadata")
    github.add_argument("repository")
    github.add_argument("kind", choices=["issues", "pulls"])
    github.add_argument("number", type=int)
    from ekm.cli_microsoft import add_microsoft_commands

    add_microsoft_commands(commands)
    return root


def auth(config: Config, profile: Profile):
    if profile.account_type == "mock":
        raise ValueError("mock profiles do not need Microsoft authentication")
    from ekm.microsoft.auth import MicrosoftAuth

    return MicrosoftAuth(
        config.profile_dir(profile),
        profile.client_id,
        profile.account_type,
        profile.tenant_id or None,
        scopes=profile.scopes,
        redirect_uri=profile.redirect_uri,
    )


def account_command(config: Config, args) -> None:
    if args.action == "add":
        profile = Profile(
            name=args.name,
            account_type=args.account_type,
            client_id=args.client_id,
            tenant_id=args.tenant_id,
            site_id=args.site_id,
            drive_id=args.drive_id,
            scopes=args.scopes,
        )
        config.add(profile)
        emit({"added": profile.name, "active": False, "next": "ekm account switch NAME --approve"})
    elif args.action == "list":
        emit(
            [dict(p.model_dump(), active=p.name == config.active_name()) for p in config.profiles()]
        )
    elif args.action == "switch":
        profile = config.get(args.name)
        emit({"operation": "switch", "destination": profile.model_dump(), "records_moved": 0})
        confirm(args, "Switch the active destination to this profile?")
        config.switch(args.name, approved=True)
        emit({"active": args.name})
    elif args.action == "remove":
        profile = config.get(args.name)
        emit(
            {
                "operation": "remove profile",
                "profile": profile.name,
                "note": "local records and backups remain for recovery; no remote data is deleted",
            }
        )
        confirm(args, "Sign out and remove this profile configuration?")
        if profile.account_type != "mock":
            auth(config, profile).logout()
        config.remove(args.name, approved=True)
        emit({"removed": args.name})
    else:
        profile = config.get(args.profile)
        if args.action == "status":
            emit(
                {
                    "profile": profile.model_dump(),
                    "active": profile.name == config.active_name(),
                    "cache": str(config.profile_dir(profile)),
                    "note": "configuration status; use init/check with an authorized account for live validation",
                }
            )
        elif args.action == "login":
            emit(auth(config, profile).login())
        else:
            auth(config, profile).logout()
            emit({"signed_out": profile.name})


def require_active(engine: Engine) -> None:
    if engine.config.active_name() != engine.profile.name:
        raise ValueError(
            "switch to this profile explicitly before writing: ekm account switch NAME --approve"
        )


def save_record(engine: Engine, args, data: dict) -> None:
    record = engine.prepare(data)
    preview = engine.preview(record, etag=args.etag)
    emit(preview)
    if args.preview_output:
        write_json(args.preview_output, record.model_dump(mode="json"))
    if args.dry_run:
        return
    require_active(engine)
    if args.command == "update" and not args.etag:
        raise ValueError("updates require --etag from ekm show")
    confirm(args, "Save this redacted record to the displayed destination?")
    stored = engine.save(
        record,
        approved=True,
        etag=args.etag,
        offline=args.offline,
        expected_hash=args.expected_hash,
    )
    emit(
        {
            "status": "queued" if stored is None else "saved",
            "id": str(record.id if stored is None else stored.record.id),
            "etag": None if stored is None else stored.etag,
        }
    )


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, default=str)
        stream.write("\n")


def load_record(args, default_kind: Kind | None = None) -> dict:
    if args.input is None:
        raise ValueError("provide --input with an agent-drafted record JSON file")
    data = json.loads(args.input.read_text())
    if not isinstance(data, dict):
        raise ValueError("expected one record object; use restore for an export bundle")
    if default_kind:
        data.setdefault("kind", default_kind.value)
        if data["kind"] != default_kind.value:
            raise ValueError(f"this workflow requires kind={default_kind.value}")
    if getattr(args, "project", None):
        data["project_id"] = args.project
    if getattr(args, "task", None):
        data["task_id"] = args.task
    return data


def review_command(engine: Engine, args) -> None:
    states = engine.questions(
        due=args.due, project=args.project, topic=args.topic, difficulty=args.difficulty
    )
    if args.answer is not None and args.question is None:
        raise ValueError("--answer requires --question ID")
    if args.question is None and not sys.stdin.isatty():
        emit(states)  # Never expose expected answers before a response.
        return
    if args.question is None:
        if not states:
            emit({"questions": [], "message": "No matching questions in the local cache"})
            return
        question_id = UUID(states[0]["question_id"])
    else:
        question_id = args.question
    question = engine.get(Kind.question, question_id).record
    emit({"question_id": str(question.id), "question": question.question})
    answer = args.answer
    if answer is None:
        if not sys.stdin.isatty():
            return
        answer = input("Your answer: ").strip()
    if not answer.strip():
        raise ValueError("an answer is required before revealing the expected answer")
    emit(
        {
            "your_answer": answer,
            "expected_answer": question.expected_answer,
            "explanation": question.explanation,
            "evaluation": "Human or agent judgment; EKM does not automatically grade correctness.",
        }
    )
    score = args.score
    if score is None:
        if not sys.stdin.isatty():
            raise ValueError("provide --score between 0 and 1 after evaluating the answer")
        score = float(input("Score (0 to 1): "))
    feedback = args.feedback
    if not feedback and sys.stdin.isatty():
        feedback = input("Feedback (correct, missing, incorrect, ideal answer): ")
    record = Record(
        kind=Kind.review,
        title=f"Review: {question.title}",
        question_id=question.id,
        user_answer=answer,
        expected_answer=question.expected_answer,
        score=score,
        feedback=feedback,
        evaluator=args.evaluator,
        review_date=utcnow(),
        project_id=question.project_id,
    ).sealed()
    emit(engine.preview(record))
    require_active(engine)
    confirm(args, "Save this review and derive the next review date?")
    stored = engine.save(record, approved=True, offline=args.offline)
    emit({"status": "queued" if stored is None else "saved", "review_id": str(record.id)})


def export_command(engine: Engine, args) -> None:
    records = engine.records(project=args.project)
    if args.format == "word":
        from ekm.documents.word import write_word

        write_word(engine.report_records(project=args.project), args.output)
    elif args.format == "excel":
        from ekm.documents.excel import write_excel

        write_excel(engine.report_records(project=args.project), args.output)
    else:
        write_json(
            args.output,
            {
                "schema_version": 1,
                "source_profile": engine.profile.name,
                "exported_at": utcnow().isoformat(),
                "records": [r.model_dump(mode="json") for r in records],
            },
        )
    emit({"written": str(args.output), "records": len(records), "source": "local cache"})
    if args.upload:
        require_active(engine)
        emit(
            {
                "operation": "upload document",
                "path": str(args.output),
                "destination": engine.profile.model_dump(),
            }
        )
        confirm(args, "Upload this document to the selected storage destination?")
        emit({"remote_document": engine.provider.upload_document(args.output, approved=True)})


def run(engine: Engine, args) -> None:
    command = args.command
    if command == "init":
        require_active(engine)
        emit({"operation": "initialize storage", "destination": engine.profile.model_dump()})
        confirm(args, "Provision EKM storage in this destination?")
        engine.provider.initialize_storage(approved=True)
        root_id = getattr(engine.provider, "root_id", None)
        if root_id:
            engine.config.set_root(engine.profile.name, root_id)
        emit({"initialized": engine.profile.name})
    elif command == "refresh":
        emit({"cached_records": engine.refresh()})
    elif command == "check":
        emit(engine.provider.check_connection())
    elif command == "sync":
        if args.action == "status":
            emit(engine.sync_status())
        elif args.action == "pull":
            result = engine.pull()
            emit(result)
            if result["conflicts"] or result["failed"]:
                raise ValueError(
                    "download queue retained; retry sync pull after inspecting the conflict"
                )
        elif args.action == "resolve":
            require_active(engine)
            if not args.kind or not args.id or not args.strategy:
                raise ValueError("sync resolve requires --kind, --id and --strategy remote|merge")
            merged = json.loads(args.input.read_text()) if args.input else None
            preview = engine.resolve(
                Kind(args.kind), args.id, strategy=args.strategy, merged=merged, dry_run=True
            )
            emit(preview)
            if args.dry_run:
                return
            confirm(
                args,
                "Resolve this conflict? The prior local draft remains in the recovery history.",
            )
            emit(
                engine.resolve(
                    Kind(args.kind),
                    args.id,
                    strategy=args.strategy,
                    merged=merged,
                    expected_etag=args.etag,
                    approved=True,
                )
            )
        else:
            require_active(engine)
            emit({"destination": engine.profile.model_dump(), "pending": engine.pending()})
            confirm(args, "Send these pending drafts to the displayed destination?")
            result = (
                engine.push(approved=True) if args.action == "push" else engine.sync(approved=True)
            )
            emit(result)
            pull = result.get("pull", {})
            if (
                result["conflicts"]
                or result["failed"]
                or pull.get("conflicts")
                or pull.get("failed")
            ):
                raise ValueError(
                    "some pending writes or downloads require resolution; see sync results"
                )
    elif command == "pending":
        emit(engine.pending())
    elif command == "discard":
        require_active(engine)
        emit(
            {
                "operation": "discard pending draft",
                "id": str(args.id),
                "profile": engine.profile.name,
            }
        )
        confirm(args, "Discard this pending local draft?")
        engine.discard(args.id, approved=True)
    elif command == "revive":
        require_active(engine)
        emit(
            {
                "operation": "clear local deletion protection",
                "id": str(args.id),
                "profile": engine.profile.name,
            }
        )
        confirm(args, "Allow this deleted record ID to be recreated by a subsequent approved save?")
        engine.revive_deleted(args.id, approved=True)
    elif command in {"log", "import", "update"}:
        if command == "log" and args.input is None:
            from ekm.integrations.git import git_context

            data = dict(
                git_context(args.repo),
                kind="task",
                title=args.title or "Engineering session draft",
                task_id=args.task or "",
                project_id=args.project,
                status="unknown",
                unanswered_questions=[
                    "What was the task, implementation, testing and engineering lesson?"
                ],
            )
            record = engine.prepare(data)
            emit(
                {
                    "draft": record.model_dump(mode="json"),
                    "next": "Enrich this draft from the available session; review and save via ekm log --input FILE",
                }
            )
            if args.preview_output:
                write_json(args.preview_output, record.model_dump(mode="json"))
        else:
            save_record(engine, args, load_record(args, Kind.task if command == "log" else None))
    elif command in {"decision", "debug", "debt", "project"}:
        kind = {
            "decision": Kind.decision,
            "debug": Kind.debugging,
            "debt": Kind.debt,
            "project": Kind.project,
        }[command]
        if args.input or args.action == "import":
            save_record(engine, args, load_record(args, kind))
        elif args.action == "show":
            if not args.value:
                raise ValueError("show requires a record UUID")
            stored = engine.get(kind, UUID(args.value))
            emit({"record": stored.record.model_dump(mode="json"), "etag": stored.etag})
        elif args.action == "search" and command == "debug":
            if not args.value:
                raise ValueError("debug search requires a query")
            emit(
                engine.search(
                    args.value, kind=kind, project=args.project, technology=args.technology
                )
            )
        elif command == "project" and args.action != "list":
            emit(engine.insights(project=args.action))
        elif args.action == "list":
            emit(
                [
                    r.model_dump(mode="json")
                    for r in engine.records(
                        kind=kind,
                        project=args.project,
                        technology=args.technology,
                        priority=getattr(args, "priority", None),
                    )
                ]
            )
        else:
            raise ValueError(
                "supported actions: list, show UUID, import --input FILE; debug also supports search QUERY"
            )
    elif command == "search":
        emit(
            engine.search(
                args.query,
                kind=Kind(args.kind) if args.kind else None,
                project=args.project,
                technology=args.technology,
            )
        )
    elif command == "show":
        stored = engine.get(Kind(args.kind), args.id, remote=args.remote)
        result = {"record": stored.record.model_dump(mode="json"), "etag": stored.etag}
        if args.related:
            result["related"] = [r.model_dump(mode="json") for r in engine.related(stored.record)]
        emit(result)
    elif command == "delete":
        require_active(engine)
        emit(
            {
                "operation": "delete",
                "kind": args.kind,
                "id": str(args.id),
                "etag": args.etag,
                "destination": engine.profile.model_dump(),
            }
        )
        confirm(args, "Permanently delete this remote record?")
        engine.delete(Kind(args.kind), args.id, args.etag, approved=True)
        emit({"deleted": str(args.id)})
    elif command in {"review", "quiz"}:
        review_command(engine, args)
    elif command in {"insights", "weekly", "monthly"}:
        days = {"weekly": 7, "monthly": 30}.get(command)
        report = engine.insights(
            project=args.project, since=utcnow() - timedelta(days=days) if days else None
        )
        emit(
            report["recurring_issues"]
            if args.recurring_issues
            else report["learning_gaps"]
            if args.learning_gaps
            else report
        )
    elif command in {"export", "backup"}:
        export_command(engine, args)
    elif command == "restore":
        bundle = json.loads(args.input.read_text())
        if (
            not isinstance(bundle, dict)
            or bundle.get("schema_version") != 1
            or not isinstance(bundle.get("records"), list)
        ):
            raise ValueError("restore requires a version-1 EKM export bundle")
        # Validate every record before the first write; keep stable IDs and never overwrite conflicts.
        records = [
            engine.prepare(verify_integrity(Record.model_validate(data)).model_dump(mode="json"))
            for data in bundle["records"]
        ]
        emit(
            {
                "operation": "restore",
                "source_profile": bundle.get("source_profile"),
                "destination": engine.profile.model_dump(),
                "revive_deleted": args.revive_deleted,
                "records": [r.model_dump(mode="json") for r in records],
            }
        )
        if args.dry_run:
            return
        require_active(engine)
        confirm(
            args,
            "Restore these records into this destination? Cross-account movement must be expressly authorized.",
        )
        for record in records:
            if args.revive_deleted:
                engine.revive_deleted(record.id, approved=True)
            engine.save(record, approved=True, offline=args.offline)
        emit({"restored": len(records), "offline": args.offline})


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    engine = None
    try:
        if args.command == "github":
            from ekm.integrations.git import github_metadata

            emit(redact(github_metadata(args.repository, args.kind, args.number)))
            return 0
        config = Config()
        if args.command in {"auth", "onedrive"}:
            from ekm.cli_microsoft import dispatch

            dispatch(config, args)
            return 0
        if args.command == "account":
            account_command(config, args)
            return 0
        engine = Engine(config, config.get(args.profile))
        run(engine, args)
        return 0
    except ValidationError as error:
        # Pydantic's default message includes input values, which could contain sensitive data.
        emit_error = [
            {"field": ".".join(map(str, e["loc"])), "reason": e["type"]}
            for e in error.errors(include_input=False)
        ]
        print("ekm: invalid record/configuration: " + json.dumps(emit_error), file=sys.stderr)
        return 1
    except (ValueError, OSError, StorageError) as error:
        print("ekm: " + str(redact(str(error))), file=sys.stderr)
        return 1
    except sqlite3.Error:
        print(
            "ekm: local cache operation failed; retain the private profile directory for recovery before repairing storage",
            file=sys.stderr,
        )
        return 1
    finally:
        if engine is not None:
            engine.close()


if __name__ == "__main__":
    raise SystemExit(main())
