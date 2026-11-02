"""Profile routing without implicit account or storage destination changes."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Literal

from filelock import FileLock
from pydantic import BaseModel, ConfigDict, field_validator, model_validator


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    account_type: Literal["mock", "personal", "company", "google"]
    client_id: str = ""
    tenant_id: str = ""
    site_id: str = ""
    drive_id: str | None = None
    root_id: str | None = None
    scopes: list[str] | None = None
    redirect_uri: str = "http://localhost"
    storage_location: Literal["approot", "drive-root"] = "approot"
    google_credentials_file: str = "credentials.json"
    google_env_file: str | None = None
    spreadsheet_id: str | None = None
    spreadsheet_url: str | None = None

    @field_validator("name")
    @classmethod
    def safe_name(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value):
            raise ValueError("profile names must be 1–64 letters, digits, hyphens or underscores")
        return value.lower()

    @model_validator(mode="after")
    def microsoft_requirements(self) -> Profile:
        if self.account_type != "mock" and not self.client_id:
            raise ValueError("OAuth profiles require an application client_id")
        if self.account_type == "company" and (not self.tenant_id or not self.site_id):
            raise ValueError("company profiles require tenant_id and site_id")
        if self.account_type == "company" and self.tenant_id in {
            "common",
            "organizations",
            "consumers",
        }:
            raise ValueError("company profiles require a specific tenant")
        return self


class Config:
    def __init__(self, home: Path | None = None):
        self.home = (
            (home or Path(os.environ.get("EKM_HOME", "~/.local/share/ekm"))).expanduser().resolve()
        )
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.home / "profiles.json"
        self.lock = FileLock(str(self.home / "profiles.lock"))

    def _read(self) -> dict:
        if not self.path.exists():
            return {"version": 1, "active": None, "profiles": {}}
        data = json.loads(self.path.read_text())
        if data.get("version") != 1:
            raise ValueError("unsupported profile configuration version")
        return data

    def _write(self, data: dict) -> None:
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, indent=2) + "\n")
        temporary.chmod(0o600)
        temporary.replace(self.path)

    def profiles(self) -> list[Profile]:
        return [Profile.model_validate(p) for p in self._read()["profiles"].values()]

    def active_name(self) -> str | None:
        return self._read()["active"]

    def get(self, name: str | None = None) -> Profile:
        data = self._read()
        name = name or data["active"]
        name = name.lower() if name else name
        if name not in data["profiles"]:
            raise ValueError("no matching profile; run ekm account add and ekm account switch")
        return Profile.model_validate(data["profiles"][name])

    def add(self, profile: Profile) -> None:
        with self.lock:
            data = self._read()
            if profile.name in data["profiles"]:
                raise ValueError(
                    "profile exists; remove explicitly before replacing its destination"
                )
            if profile.name in data.get("retired_names", []):
                raise ValueError(
                    "profile name is retired to protect isolated retained data; choose a new name"
                )
            data["profiles"][profile.name] = profile.model_dump()
            self._write(data)

    def switch(self, name: str, *, approved: bool = False) -> None:
        name = name.lower()
        if not approved:
            raise ValueError("account switching requires explicit approval")
        with self.lock:
            self.get(name)
            data = self._read()
            data["active"] = name
            self._write(data)

    def set_root(self, name: str, root_id: str) -> None:
        name = name.lower()
        with self.lock:
            data = self._read()
            data["profiles"][name]["root_id"] = root_id
            self._write(data)

    def remove(self, name: str, *, approved: bool = False) -> None:
        name = name.lower()
        if not approved:
            raise ValueError("profile removal requires explicit approval")
        with self.lock:
            self.get(name)
            data = self._read()
            del data["profiles"][name]
            data.setdefault("retired_names", []).append(name)
            if data["active"] == name:
                data["active"] = None
            self._write(data)
        # Retain isolated data for explicit recovery. Tokens must be signed out first by CLI.

    def set_google_dashboard(self, name: str, spreadsheet_id: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", spreadsheet_id):
            raise ValueError("invalid Google spreadsheet ID")
        name = name.lower()
        with self.lock:
            if self.get(name).account_type != "google":
                raise ValueError("Google dashboard IDs belong only to Google profiles")
            data = self._read()
            data["profiles"][name]["spreadsheet_id"] = spreadsheet_id
            data["profiles"][name]["spreadsheet_url"] = (
                f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"
            )
            self._write(data)

    def profile_dir(self, profile: Profile) -> Path:
        directory = self.home / "profiles" / profile.name
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        return directory
