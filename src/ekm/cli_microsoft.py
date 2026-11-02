"""Personal-account command adapters over the existing shared engine."""

from __future__ import annotations

import hashlib
from pathlib import Path

from ekm.config import Config, Profile
from ekm.core import Engine
from ekm.microsoft.auth import MicrosoftAuth
from ekm.microsoft.graph_client import GraphClient
from ekm.microsoft.settings import personal_profile
from ekm.models import utcnow
from ekm.providers.base import StorageError

DOCUMENT_FOLDERS = ("journals", "dashboards", "backups", "reports")


def add_microsoft_commands(commands) -> None:
    auth = commands.add_parser("auth", help="personal Microsoft sign-in and safe session status")
    actions = auth.add_subparsers(dest="auth_action", required=True)
    login = actions.add_parser("login")
    login.add_argument("--flow", choices=["interactive", "device"], default="interactive")
    login.add_argument(
        "--name", help="new/existing personal profile; defaults to personal on first setup"
    )
    login.add_argument("--env-file", type=Path)
    login.add_argument("--approve", action="store_true")
    actions.add_parser("status")
    actions.add_parser("logout")
    drive = commands.add_parser("onedrive", help="application-owned personal OneDrive storage")
    actions = drive.add_subparsers(dest="onedrive_action", required=True)
    initialize = actions.add_parser("initialize")
    initialize.add_argument("--approve", action="store_true")
    initialize.add_argument("--adopt-existing", action="store_true")
    test = actions.add_parser("test")
    test.add_argument("--approve", action="store_true")
    sync = actions.add_parser("sync")
    sync.add_argument("--approve", action="store_true")
    backup = actions.add_parser("backup")
    backup.add_argument("--output", type=Path, required=True)
    backup.add_argument("--approve", action="store_true")
    upload = actions.add_parser("upload")
    upload.add_argument("--input", type=Path, required=True)
    upload.add_argument("--etag")
    upload.add_argument("--approve", action="store_true")
    download = actions.add_parser("download")
    metadata = actions.add_parser("metadata")
    for command in (download, metadata):
        command.add_argument("--folder", choices=DOCUMENT_FOLDERS, required=True)
        command.add_argument("--name", required=True)
    download.add_argument("--output", type=Path, required=True)


def _auth(config: Config, profile: Profile) -> MicrosoftAuth:
    if profile.account_type != "personal":
        raise ValueError("select a personal profile; this command does not support company storage")
    return MicrosoftAuth(
        config.profile_dir(profile),
        profile.client_id,
        profile.account_type,
        scopes=profile.scopes,
        redirect_uri=profile.redirect_uri,
    )


def _active(config: Config, profile: Profile) -> None:
    if profile.name != config.active_name():
        raise ValueError("switch to this profile before writing: ekm account switch NAME --approve")


def _login_profile(config: Config, args) -> Profile:
    from ekm.cli import confirm, emit

    selected = getattr(args, "profile", None)
    if selected:
        profile = config.get(selected)
        _active(config, profile)
        return profile
    if args.name:
        existing = {p.name: p for p in config.profiles()}
        profile = existing.get(args.name.lower())
        if profile is not None and profile.account_type != "personal":
            raise ValueError("the requested profile is not a personal Microsoft destination")
        if profile is None:
            profile = personal_profile(args.name, args.env_file)
            emit(
                {
                    "operation": "create and activate personal profile",
                    "destination": profile.model_dump(),
                }
            )
            confirm(
                args, "Create and activate this personal destination? No records will be moved."
            )
            config.add(profile)
            config.switch(profile.name, approved=True)
        elif profile.name != config.active_name():
            emit({"operation": "switch personal profile", "destination": profile.model_dump()})
            confirm(args, "Activate this personal destination? No records will be moved.")
            config.switch(profile.name, approved=True)
        return profile
    if config.active_name():
        profile = config.get()
        if profile.account_type != "personal":
            raise ValueError(
                "active profile is not personal; use ekm auth login --name personal --approve to configure an isolated personal destination"
            )
        return profile
    existing_personal = next((p for p in config.profiles() if p.name == "personal"), None)
    if existing_personal:
        profile = existing_personal
        if profile.account_type != "personal":
            raise ValueError(
                "the existing personal-named profile is not a Microsoft personal destination"
            )
        emit({"operation": "activate personal profile", "destination": profile.model_dump()})
        confirm(args, "Activate this personal destination? No records will be moved.")
    else:
        profile = personal_profile("personal", args.env_file)
        emit(
            {
                "operation": "create and activate personal profile",
                "destination": profile.model_dump(),
            }
        )
        confirm(args, "Create and activate this personal destination? No records will be moved.")
        config.add(profile)
    config.switch(profile.name, approved=True)
    return profile


def _connectivity(graph: GraphClient, *, identify: bool = False) -> dict:
    result = {}
    if identify:
        user = graph.request("GET", "/me").json()
        result["user"] = {k: user.get(k) for k in ("id", "displayName", "userPrincipalName")}
    drive = graph.request("GET", "/me/drive").json()
    result.update(
        {"connected": True, "drive_id": drive.get("id"), "drive_type": drive.get("driveType")}
    )
    return result


def _auth_command(config: Config, args) -> None:
    from ekm.cli import emit

    if args.auth_action == "login":
        profile = _login_profile(config, args)
        session = _auth(config, profile)
        result = session.login(flow=args.flow)
        graph = GraphClient(session.access_token)
        try:
            connection = _connectivity(graph, identify=True)
        finally:
            graph.close()
        emit(
            {
                "profile": profile.name,
                "authenticated": True,
                "account": result,
                "onedrive": connection,
                "next": "ekm onedrive initialize --approve",
            }
        )
        return
    if not getattr(args, "profile", None) and config.active_name() is None:
        emit(
            {
                "authenticated": False,
                "configured": False,
                "account_type": "personal",
                "message": "Paste MICROSOFT_CLIENT_ID into .env, then run ekm auth login --approve",
            }
        )
        return
    profile = config.get(getattr(args, "profile", None))
    session = _auth(config, profile)
    if args.auth_action == "logout":
        _active(config, profile)
        session.logout()
        emit({"signed_out": profile.name, "remote_files_modified": False})
        return
    status = session.status()
    status["profile"] = profile.name
    status["onedrive"] = {"connected": False, "checked": False}
    if status.get("authenticated"):
        graph = GraphClient(session.access_token)
        try:
            status["onedrive"] = {**_connectivity(graph), "checked": True}
        except StorageError as error:
            status["onedrive"] = {"connected": False, "checked": True, "message": str(error)}
        finally:
            graph.close()
    emit(status)


def _ensure_pull(result: dict) -> None:
    if result.get("conflicts") or result.get("failed") or result.get("queued"):
        raise StorageError(
            "Remote snapshot is incomplete; resolve the reported pull failures before backup"
        )


def _onedrive_command(config: Config, args) -> None:
    from ekm.cli import confirm, emit, write_json

    profile = config.get(getattr(args, "profile", None))
    if profile.account_type != "personal":
        raise ValueError("select a personal profile for OneDrive commands")
    engine = Engine(config, profile)
    action = args.onedrive_action
    try:
        if action in {"download", "metadata"}:
            if action == "download":
                emit(engine.provider.download_document(args.folder, args.name, args.output))
            else:
                emit(engine.provider.document_metadata(args.folder, args.name))
            return
        _active(config, profile)
        preview = {"operation": f"onedrive {action}", "destination": profile.model_dump()}
        if action == "initialize":
            preview["adopt_existing"] = args.adopt_existing
        elif action == "sync":
            preview["pending"] = engine.pending()
        elif action == "upload":
            if args.input.suffix.lower() not in {".docx", ".xlsx"}:
                raise ValueError(
                    "upload accepts Word/Excel files; use onedrive backup for structured JSON"
                )
            preview.update(
                path=str(args.input),
                content_hash=hashlib.sha256(args.input.read_bytes()).hexdigest(),
                etag=args.etag,
            )
        elif action == "backup":
            preview["local_output"] = str(args.output)
        elif action == "test":
            preview["effects"] = (
                "create, read, and delete only a unique temporary EKM test file under the owned folder"
            )
        emit(preview)
        confirm(args, "Approve this operation in the displayed application-owned destination?")
        if action == "initialize":
            engine.provider.initialize_storage(approved=True, adopt_existing=args.adopt_existing)
            config.set_root(profile.name, engine.provider.root_id)
            emit({"initialized": profile.name, "root_id": engine.provider.root_id})
        elif action == "test":
            emit(engine.provider.test_connection(approved=True))
        elif action == "sync":
            result = engine.sync(approved=True)
            emit(result)
            if result.get("conflicts") or result.get("failed"):
                raise StorageError("some synchronization operations need explicit resolution")
            _ensure_pull(result.get("pull", {}))
        elif action == "backup":
            pull = engine.pull()
            emit({"pull": pull})
            _ensure_pull(pull)
            # Pull can retain cache rows with pending local work. Back up the complete
            # validated remote listing rather than those potentially stale rows.
            records = [
                engine.prepare(data).model_dump(mode="json")
                for data in engine.provider.export_data()
            ]
            bundle = {
                "schema_version": 1,
                "source_profile": profile.name,
                "exported_at": utcnow().isoformat(),
                "records": records,
            }
            write_json(args.output, bundle)
            emit(
                {
                    "local_backup": str(args.output),
                    "records": len(records),
                    "remote_backup": engine.provider.backup_data(
                        records, args.output.name, approved=True
                    ),
                }
            )
        elif action == "upload":
            if args.etag:
                emit(engine.provider.update_document(args.input, args.etag, approved=True))
            else:
                emit(engine.provider.upload_file(args.input, approved=True))
    finally:
        engine.close()


def dispatch(config: Config, args) -> bool:
    if args.command == "auth":
        _auth_command(config, args)
        return True
    if args.command == "onedrive":
        _onedrive_command(config, args)
        return True
    return False
