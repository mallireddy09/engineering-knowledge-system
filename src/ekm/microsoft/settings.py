"""Read only the public, allowlisted Microsoft application settings; never execute .env."""

import os
import shlex
from pathlib import Path
from uuid import UUID

from ekm.config import Profile
from ekm.microsoft.auth import MicrosoftAuth

KEYS = {
    "MICROSOFT_CLIENT_ID",
    "MICROSOFT_ACCOUNT_TYPE",
    "MICROSOFT_REDIRECT_URI",
    "MICROSOFT_SCOPES",
    "EKM_STORAGE_PROVIDER",
}


def personal_profile(name: str = "personal", env_file: Path | None = None) -> Profile:
    values = {}
    path = env_file or Path.cwd() / ".env"
    if path.is_file():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, separator, raw = line.removeprefix("export ").partition("=")
            key = key.strip()
            if separator and key in KEYS:
                # Parse quotes/comments only. No shell evaluation or variable interpolation.
                parts = shlex.split(raw, comments=True, posix=True)
                values[key] = " ".join(parts)
    values.update({key: os.environ[key] for key in KEYS if key in os.environ})
    client_id = values.get("MICROSOFT_CLIENT_ID", "")
    try:
        UUID(client_id)
    except ValueError as error:
        raise ValueError(
            "Paste your existing Application (Client) ID into MICROSOFT_CLIENT_ID in .env"
        ) from error
    if values.get("MICROSOFT_ACCOUNT_TYPE", "personal") != "personal":
        raise ValueError("this configuration requires MICROSOFT_ACCOUNT_TYPE=personal")
    if values.get("EKM_STORAGE_PROVIDER", "onedrive") != "onedrive":
        raise ValueError("this configuration requires EKM_STORAGE_PROVIDER=onedrive")
    scopes = values.get("MICROSOFT_SCOPES", "User.Read Files.ReadWrite").split()
    if set(scopes) != {"User.Read", "Files.ReadWrite"}:
        raise ValueError(
            "use MICROSOFT_SCOPES=User.Read Files.ReadWrite for the dedicated OneDrive folder"
        )
    redirect_uri = values.get("MICROSOFT_REDIRECT_URI", "http://localhost")
    MicrosoftAuth._redirect_port(redirect_uri)
    return Profile(
        name=name,
        account_type="personal",
        client_id=client_id,
        scopes=scopes,
        redirect_uri=redirect_uri,
        storage_location="drive-root",
    )
