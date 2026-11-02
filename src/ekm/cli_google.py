"""Google adapters and destination selection over the shared EKM engine."""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import UUID

from ekm.config import Config, Profile
from ekm.core import Engine
from ekm.google.auth import GoogleAuth
from ekm.google.settings import google_profile
from ekm.models import Kind, utcnow
from ekm.providers.base import StorageError


def add_google_commands(commands, auth_actions) -> None:
    google_auth = auth_actions.add_parser("google", help="independent Google OAuth session")
    actions = google_auth.add_subparsers(dest="google_auth_action", required=True)
    login = actions.add_parser("login")
    login.add_argument("--name")
    login.add_argument("--env-file", type=Path)
    login.add_argument("--approve", action="store_true")
    login.add_argument(
        "--no-browser",
        action="store_true",
        help="show the Google authorization link for manual opening",
    )
    actions.add_parser("status")
    actions.add_parser("logout")
    provider = commands.add_parser("provider", help="select a destination without moving records")
    actions = provider.add_subparsers(dest="provider_action", required=True)
    actions.add_parser("list")
    actions.add_parser("status")
    switch = actions.add_parser("switch")
    switch.add_argument("provider_name", choices=["google", "microsoft"])
    switch.add_argument("--name", help="choose a profile when multiple accounts are configured")
    switch.add_argument("--approve", action="store_true")
    google = commands.add_parser("google", help="Google Drive and native Docs/Sheets")
    actions = google.add_subparsers(dest="google_action", required=True)
    for name in ("initialize", "journal", "dashboard", "upload", "backup", "resolve"):
        action = actions.add_parser(name)
        action.add_argument("--approve", action="store_true")
        if name == "journal":
            action.add_argument("task_id", type=UUID)
        elif name in {"upload", "resolve"}:
            action.add_argument("--input", type=Path, required=name == "upload")
        elif name == "backup":
            action.add_argument("--output", type=Path, required=True)
        if name == "resolve":
            action.add_argument("kind", choices=[k.value for k in Kind])
            action.add_argument("id", type=UUID)
            action.add_argument("--head", action="append", required=True)
            action.add_argument("--delete", action="store_true")
    for name in ("test", "conflicts", "metadata"):
        action = actions.add_parser(name)
        if name == "metadata":
            action.add_argument("file_id")
    download = actions.add_parser("download")
    download.add_argument("file_id")
    download.add_argument("--output", type=Path, required=True)


def _session(config: Config, profile: Profile) -> GoogleAuth:
    if profile.account_type != "google":
        raise ValueError("select a Google profile; Microsoft authentication remains independent")
    return GoogleAuth(
        config.profile_dir(profile),
        Path(profile.google_credentials_file),
        env_file=Path(profile.google_env_file) if profile.google_env_file else None,
    )


def _family(profile: Profile) -> str:
    return {"google": "google", "personal": "microsoft", "company": "microsoft"}.get(
        profile.account_type, "mock"
    )


def _active(config: Config, profile: Profile) -> None:
    if config.active_name() != profile.name:
        raise ValueError("switch to this destination explicitly before writing")


def _login_profile(config: Config, args) -> Profile:
    from ekm.cli import confirm, emit

    selected = args.profile or args.name
    existing = {p.name: p for p in config.profiles()}
    if selected:
        selected = selected.lower()
        profile = existing.get(selected)
        if profile and profile.account_type != "google":
            raise ValueError(
                "this profile belongs to a different provider; choose a new Google name"
            )
    elif config.active_name():
        profile = config.get()
        if profile.account_type != "google":
            raise ValueError(
                "use ekm auth google login --name google --approve to add a separate destination"
            )
    else:
        profile = existing.get("google")
        if profile and profile.account_type != "google":
            raise ValueError("the google-named profile belongs to a different provider")
    if profile is None:
        profile = google_profile(selected or "google", args.env_file)
        emit(
            {
                "operation": "create Google profile and activate destination",
                "profile": profile.model_dump(),
                "records_moved": 0,
            }
        )
        confirm(args, "Create and activate this Google destination?")
        config.add(profile)
        config.switch(profile.name, approved=True)
    elif profile.name != config.active_name():
        emit(
            {"operation": "switch Google destination", "profile": profile.name, "records_moved": 0}
        )
        confirm(args, "Switch to this Google destination?")
        config.switch(profile.name, approved=True)
    return profile


def _auth(config: Config, args) -> None:
    from ekm.cli import emit

    if args.google_auth_action == "login":
        profile = _login_profile(config, args)
        session = _session(config, profile)
        emit(session.login(open_browser=False) if args.no_browser else session.login())
    elif config.active_name() is None and args.profile is None:
        emit(
            {
                "authenticated": False,
                "provider": "google",
                "configured": False,
                "message": "Configure GOOGLE_CREDENTIALS_FILE, then run ekm auth google login --name google --approve",
            }
        )
    else:
        profile = config.get(args.profile)
        session = _session(config, profile)
        if args.google_auth_action == "logout":
            _active(config, profile)
            session.logout()
            emit({"signed_out": profile.name, "remote_files_modified": False})
        else:
            emit(session.status())


def _provider(config: Config, args) -> None:
    from ekm.cli import confirm, emit

    if args.provider_action == "list":
        emit(
            {
                "supported": ["microsoft", "google"],
                "profiles": [
                    {
                        "name": p.name,
                        "provider": _family(p),
                        "active": p.name == config.active_name(),
                    }
                    for p in config.profiles()
                ],
            }
        )
    elif args.provider_action == "status":
        if config.active_name() is None and args.profile is None:
            emit({"active": None})
        else:
            profile = config.get(args.profile)
            emit(
                {
                    "profile": profile.name,
                    "provider": _family(profile),
                    "active": profile.name == config.active_name(),
                    "remote_checked": False,
                }
            )
    else:
        choices = [
            p
            for p in config.profiles()
            if _family(p) == args.provider_name and (not args.name or p.name == args.name.lower())
        ]
        if len(choices) != 1:
            raise ValueError(
                "configure this provider first, or select one configured account with --name"
            )
        profile = choices[0]
        emit(
            {
                "operation": "switch destination",
                "provider": args.provider_name,
                "profile": profile.name,
                "records_moved": 0,
                "records_deleted": 0,
            }
        )
        confirm(args, "Activate this destination without moving historical records?")
        config.switch(profile.name, approved=True)


def _google(config: Config, args) -> None:
    from ekm.cli import confirm, emit, write_json
    from ekm.google.documents import GoogleDocuments

    profile = config.get(args.profile)
    if profile.account_type != "google":
        raise ValueError("select a Google profile using ekm provider switch google --approve")
    engine = Engine(config, profile)
    action = args.google_action
    try:
        if action == "test":
            emit(engine.provider.check_connection())
            return
        if action == "conflicts":
            emit(engine.provider.conflicts())
            return
        if action in {"metadata", "download"}:
            metadata = _owned_item(engine.provider, args.file_id)
            if action == "metadata":
                emit({k: metadata.get(k) for k in ("id", "name", "mimeType", "version", "parents")})
            else:
                native_types = {
                    "application/vnd.google-apps.document": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    "application/vnd.google-apps.spreadsheet": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                }
                if metadata["mimeType"] in native_types:
                    mime = (
                        "application/pdf"
                        if args.output.suffix.lower() == ".pdf"
                        else native_types[metadata["mimeType"]]
                    )
                    content = engine.provider.client.request(
                        "GET",
                        f"https://www.googleapis.com/drive/v3/files/{metadata['id']}/export",
                        params={"mimeType": mime},
                    ).content
                else:
                    content = engine.provider.client.download(args.file_id)
                args.output.parent.mkdir(parents=True, exist_ok=True)
                descriptor = os.open(args.output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(content)
                emit({"downloaded": args.file_id, "output": str(args.output)})
            return
        _active(config, profile)
        emit(
            {
                "operation": f"google {action}",
                "destination": profile.name,
                "task_id": str(getattr(args, "task_id", "")),
                "effect": "journal also links the Google document ID and URL to its task"
                if action == "journal"
                else action,
            }
        )
        confirm(args, "Approve this operation in the selected Google destination?")
        if action in {"journal", "dashboard"}:
            pull = engine.pull()
            if pull.get("conflicts") or pull.get("failed"):
                emit({"pull": pull})
                raise StorageError(
                    "Google snapshot is incomplete; no native document was generated"
                )
        if action == "initialize":
            engine.provider.initialize_storage(approved=True)
            config.set_root(profile.name, engine.provider.root_id)
            emit({"initialized": profile.name, "root_id": engine.provider.root_id})
        elif action == "upload":
            emit({"url": engine.provider.upload_document(args.input, approved=True)})
        elif action == "journal":
            stored = engine.get(Kind.task, args.task_id, remote=True)
            task = stored.record
            result = GoogleDocuments(engine.provider).journal(
                task, engine.related(task), approved=True
            )
            emit(result)
            references = list(
                dict.fromkeys([*task.references, f"google-doc-id:{result['id']}", result["url"]])
            )
            if references != task.references:
                linked = task.model_copy(
                    update={"references": references, "updated_at": utcnow()}
                ).sealed()
                engine.save(linked, etag=stored.etag, approved=True)
        elif action == "dashboard":
            result = GoogleDocuments(engine.provider).dashboard(
                engine.report_records(), profile.spreadsheet_id, approved=True
            )
            config.set_google_dashboard(profile.name, result["id"])
            emit(result)
        elif action == "backup":
            records = [
                engine.prepare(r).model_dump(mode="json") for r in engine.provider.export_data()
            ]
            write_json(
                args.output,
                {
                    "schema_version": 1,
                    "source_profile": profile.name,
                    "exported_at": utcnow().isoformat(),
                    "records": records,
                },
            )
            emit(
                {
                    "local_backup": str(args.output),
                    "url": engine.provider.upload_document(args.output, approved=True),
                }
            )
        elif action == "resolve":
            if bool(args.input) == bool(args.delete):
                raise ValueError("choose exactly one of --input merged.json or --delete")
            merged = engine.prepare(json.loads(args.input.read_text())) if args.input else None
            result = engine.provider.resolve_conflict(
                Kind(args.kind), args.id, args.head, merged, approved=True
            )
            emit(
                {
                    "remote_resolved": str(args.id),
                    "deleted": result is None,
                    "etag": result.etag if result else None,
                }
            )
            pull = engine.pull()
            pending = [p for p in engine.sync_status()["upload_queue"] if p["id"] == str(args.id)]
            emit(
                {
                    "pull": pull,
                    "local_pending_retained": pending,
                    "next": "Inspect retained drafts with ekm pending; use ekm sync resolve to reconcile local work against the new remote head"
                    if pending
                    else None,
                }
            )
            if pull.get("conflicts") or pull.get("failed"):
                raise StorageError(
                    "Google remote merge was saved; cache refresh is incomplete. Retry ekm sync pull"
                )
    finally:
        engine.close()


def _owned_item(provider, file_id: str) -> dict:
    """Verify both private ownership and the current pinned folder before file reads."""
    from ekm.providers.google_drive import KINDS

    metadata = provider.client.file_metadata(file_id)
    role = metadata.get("appProperties", {}).get("ekmRole")
    if role in {"journal", "dashboard"}:
        folder = provider.ensure_folder("journals" if role == "journal" else "dashboards")
        return provider.verify_file(file_id, role, parent_id=folder)
    if role == "record":
        try:
            kind = Kind(metadata["appProperties"]["ekmKind"])
        except (KeyError, ValueError) as error:
            raise StorageError("Google record metadata is invalid") from error
        return provider.verify_file(file_id, role, parent_id=provider.ensure_folder(KINDS[kind]))
    if role == "artifact":
        parents = metadata.get("parents", [])
        for name in ("journals", "dashboards", "backups", "reports"):
            folder = provider.ensure_folder(name)
            if parents == [folder]:
                return provider.verify_file(file_id, role, parent_id=folder)
    raise StorageError("Google file is outside this application's supported owned folders")


def dispatch(config: Config, args) -> None:
    if args.command == "provider":
        _provider(config, args)
    elif args.command == "auth":
        _auth(config, args)
    else:
        _google(config, args)
