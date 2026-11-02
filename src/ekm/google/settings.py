"""Public profile settings and validated desktop OAuth configuration."""

from __future__ import annotations

import json
import os
import shlex
from pathlib import Path
from urllib.parse import urlsplit

from ekm.config import Profile
from ekm.providers.base import StorageError

KEYS = {"GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_CREDENTIALS_FILE"}
TOKEN_URI = "https://oauth2.googleapis.com/token"
AUTH_URIS = {
    "https://accounts.google.com/o/oauth2/auth",
    "https://accounts.google.com/o/oauth2/v2/auth",
}


def read_google_settings(env_file: Path | None = None) -> dict[str, str]:
    values = {}
    path = env_file or Path.cwd() / ".env"
    if path.is_file():
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                key, separator, raw = line.strip().removeprefix("export ").partition("=")
                key = key.strip()
                if separator and key in KEYS:
                    values[key] = " ".join(shlex.split(raw, comments=True, posix=True))
        except (OSError, ValueError) as error:
            raise StorageError(
                "Google settings could not be read; check the ignored .env syntax."
            ) from error
    values.update({key: os.environ[key] for key in KEYS if key in os.environ})
    return values


def validate_client_config(data: dict) -> dict:
    if (
        not isinstance(data, dict)
        or set(data) != {"installed"}
        or not isinstance(data["installed"], dict)
    ):
        raise StorageError(
            "Use Google OAuth credentials for a Desktop app, not web or service-account credentials."
        )
    installed = data["installed"]
    client_id, secret = installed.get("client_id"), installed.get("client_secret")
    if (
        not isinstance(client_id, str)
        or not client_id.endswith(".apps.googleusercontent.com")
        or not client_id.strip()
    ):
        raise StorageError("Configure the existing Google Desktop OAuth client ID.")
    if not isinstance(secret, str) or not secret.strip():
        raise StorageError(
            "Configure the Google Desktop OAuth client secret in the ignored credentials file or environment."
        )
    if installed.get("auth_uri") not in AUTH_URIS or installed.get("token_uri") != TOKEN_URI:
        raise StorageError(
            "Google OAuth credentials contain an unsupported authorization or token endpoint."
        )
    redirects = installed.get("redirect_uris", [])
    if not isinstance(redirects, list) or not redirects:
        raise StorageError("Google Desktop OAuth credentials must allow a localhost redirect.")
    for uri in redirects:
        try:
            parsed = urlsplit(uri)
            port = parsed.port
        except (TypeError, ValueError) as error:
            raise StorageError(
                "Google Desktop OAuth redirects must use a local HTTP callback."
            ) from error
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
            or (port is not None and not 1 <= port <= 65535)
        ):
            raise StorageError("Google Desktop OAuth redirects must use a local HTTP callback.")
    # Ignore extra Google-console metadata rather than passing arbitrary OAuth options through.
    return {
        "installed": {
            key: installed[key]
            for key in ("client_id", "client_secret", "auth_uri", "token_uri", "redirect_uris")
        }
    }


def load_client_config(credentials_file: Path, *, env_file: Path | None = None) -> dict:
    values = read_google_settings(env_file)
    path = Path(credentials_file).expanduser()
    try:
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
        else:
            data = {
                "installed": {
                    "client_id": values.get("GOOGLE_CLIENT_ID", ""),
                    "client_secret": values.get("GOOGLE_CLIENT_SECRET", ""),
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": TOKEN_URI,
                    "redirect_uris": ["http://localhost"],
                }
            }
    except (OSError, ValueError) as error:
        raise StorageError(
            "Google desktop credentials could not be read; check the configured ignored credentials file."
        ) from error
    result = validate_client_config(data)
    if (
        values.get("GOOGLE_CLIENT_ID")
        and values["GOOGLE_CLIENT_ID"] != result["installed"]["client_id"]
    ):
        raise StorageError(
            "GOOGLE_CLIENT_ID differs from the credentials file; use one consistent existing Desktop app."
        )
    return result


def google_profile(name: str = "google", env_file: Path | None = None) -> Profile:
    values = read_google_settings(env_file)
    path = Path(values.get("GOOGLE_CREDENTIALS_FILE") or "credentials.json").expanduser()
    if not path.is_absolute() and env_file is not None:
        path = Path(env_file).resolve().parent / path
    path = path.resolve()
    settings = load_client_config(path, env_file=env_file)
    return Profile(
        name=name,
        account_type="google",
        client_id=settings["installed"]["client_id"],
        google_credentials_file=str(path),
        google_env_file=str((env_file or Path.cwd() / ".env").resolve()),
    )
