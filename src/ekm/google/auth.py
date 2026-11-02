"""Desktop Google OAuth with provider-isolated OS-encrypted token persistence."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from contextlib import contextmanager
from datetime import timezone
from pathlib import Path
from threading import RLock

import httpx
from filelock import FileLock
from google.auth.transport.requests import Request
from google.oauth2 import id_token
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from msal_extensions import build_encrypted_persistence
from msal_extensions.persistence import PersistenceNotFound

from ekm.google.settings import TOKEN_URI, load_client_config
from ekm.providers.base import StorageError

SCOPES = (
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/drive.file",
)
_DIAGNOSTIC_LOCK = RLock()
_OAUTH_LOGGERS = ("oauthlib", "requests_oauthlib", "google_auth_oauthlib", "google.auth")


@contextmanager
def _suppress_oauth_diagnostics():
    """Redact SDK callback/code/token diagnostics only during sensitive operations.

    SDK children can emit the localhost callback query or token-exchange bodies at
    INFO/DEBUG. The scoped factory catches children created during the operation;
    unrelated application records and the previous logging configuration survive.
    """
    with _DIAGNOSTIC_LOCK:
        previous = logging.getLogRecordFactory()

        def factory(*args, **kwargs):
            record = previous(*args, **kwargs)
            if any(
                record.name == prefix or record.name.startswith(prefix + ".")
                for prefix in _OAUTH_LOGGERS
            ):
                record.msg = "Google OAuth SDK diagnostic suppressed"
                record.args = ()
                record.exc_info = record.exc_text = record.stack_info = None
            return record

        logging.setLogRecordFactory(factory)
        try:
            yield
        finally:
            logging.setLogRecordFactory(previous)


class GoogleAuth:
    def __init__(self, profile_dir: Path, credentials_file: Path, *, env_file: Path | None = None):
        self.directory = Path(profile_dir)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.config = load_client_config(Path(credentials_file), env_file=env_file)
        self.client_id = self.config["installed"]["client_id"]
        self.binding_path = self.directory / "google-identity.json"
        self.lock = FileLock(str(self.directory / ".google-auth.lock"))
        self._verified_token_identity = None
        try:
            self.persistence = build_encrypted_persistence(
                str(self.directory / "google-token-cache.bin")
            )
            if not self.persistence.is_encrypted:
                raise StorageError("Google token persistence is not encrypted.")
        except Exception as error:
            raise StorageError(
                "Encrypted Google token storage is unavailable. Configure the OS keychain or libsecret; plaintext fallback is disabled."
            ) from error

    def bound_identity(self) -> dict | None:
        try:
            value = json.loads(self.binding_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as error:
            raise StorageError(
                "Google identity binding is invalid; repair this profile before signing in."
            ) from error
        if (
            not isinstance(value, dict)
            or any(not isinstance(value.get(k), str) or not value[k] for k in ("sub", "client_id"))
            or value["client_id"] != self.client_id
        ):
            raise StorageError("Google identity binding does not match this application's profile.")
        return {key: value.get(key) for key in ("sub", "client_id", "email")}

    def _validate_identity(self, claims: dict) -> dict:
        if (
            not isinstance(claims, dict)
            or not isinstance(claims.get("sub"), str)
            or not claims["sub"]
        ):
            raise StorageError(
                "Google omitted the authenticated account identity; no knowledge was accessed."
            )
        result = {
            "sub": claims["sub"],
            "client_id": self.client_id,
            "email": claims.get("email") if isinstance(claims.get("email"), str) else None,
        }
        bound = self.bound_identity()
        if bound and (bound["sub"], bound["client_id"]) != (result["sub"], result["client_id"]):
            raise StorageError(
                "This Google profile is bound to another account. Create a separate profile instead."
            )
        return result

    def _write_binding(self, identity: dict) -> None:
        fd, name = tempfile.mkstemp(prefix=".google-identity-", dir=self.directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(identity, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.binding_path)
        finally:
            Path(name).unlink(missing_ok=True)

    @staticmethod
    def _userinfo(token: str) -> dict:
        try:
            response = httpx.get(
                "https://openidconnect.googleapis.com/v1/userinfo",
                headers={"Authorization": "Bearer " + token},
                timeout=20,
                follow_redirects=False,
            )
            if response.status_code != 200:
                raise StorageError(
                    "Google account verification failed; sign in again to the same profile."
                )
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError
            return result
        except (httpx.HTTPError, ValueError) as error:
            raise StorageError(
                "Google account verification is unavailable; check connectivity and sign in again if needed."
            ) from error

    def _store(self, credentials) -> None:
        try:
            self.persistence.save(credentials.to_json())
        except Exception as error:
            raise StorageError(
                "Google session could not be saved in encrypted storage; no plaintext cache was written."
            ) from error

    @staticmethod
    def _require_scopes(credentials) -> None:
        granted = credentials.granted_scopes
        if granted is not None and not set(SCOPES).issubset(set(granted)):
            raise StorageError(
                "Google did not grant the required identity and drive.file permissions; sign in and consent again."
            )

    def _load(self):
        try:
            text = self.persistence.load()
        except PersistenceNotFound:
            raise StorageError("Google is not signed in; run ekm auth google login.") from None
        except Exception as error:
            raise StorageError(
                "Google encrypted session could not be read; check the OS credential store."
            ) from error
        try:
            data = json.loads(text)
            if (
                not isinstance(data, dict)
                or data.get("client_id") != self.client_id
                or data.get("token_uri", TOKEN_URI) != TOKEN_URI
            ):
                raise ValueError
            if data.get("signed_out"):
                raise StorageError("Google is signed out; run ekm auth google login.")
            data["token_uri"] = TOKEN_URI
            credentials = Credentials.from_authorized_user_info(data, scopes=SCOPES)
            return credentials
        except (ValueError, TypeError, KeyError) as error:
            raise StorageError(
                "Google encrypted session is invalid; sign in again to this profile."
            ) from error

    @_suppress_oauth_diagnostics()
    def login(self, *, open_browser: bool = True) -> dict:
        with self.lock:
            try:
                flow = InstalledAppFlow.from_client_config(
                    self.config, scopes=SCOPES, autogenerate_code_verifier=True
                )
                credentials = flow.run_local_server(
                    host="localhost",
                    port=0,
                    open_browser=open_browser,
                    timeout_seconds=180,
                    authorization_prompt_message=(
                        "Opening Google sign-in in your browser..."
                        if open_browser
                        else "Open this Google sign-in link in your browser:\n{url}"
                    ),
                    success_message="Google sign-in completed. You may close this window.",
                    access_type="offline",
                    prompt="consent",
                )
                claims = id_token.verify_oauth2_token(
                    credentials.id_token, Request(), audience=self.client_id
                )
                identity = self._validate_identity(claims)
                if not credentials.token or not credentials.refresh_token:
                    raise StorageError(
                        "Google did not grant an offline session; sign in and consent again."
                    )
                self._require_scopes(credentials)
                current = self._validate_identity(self._userinfo(credentials.token))
                if current["sub"] != identity["sub"]:
                    raise StorageError("Google sign-in identities differ; no session was saved.")
                self._write_binding(identity)
                self._store(credentials)
                self._verified_token_identity = (
                    hashlib.sha256(credentials.token.encode()).hexdigest(),
                    identity["sub"],
                )
                return {
                    "authenticated": True,
                    "account": identity,
                    "provider": "google",
                    "granted_scopes": list(credentials.granted_scopes or credentials.scopes or []),
                }
            except StorageError:
                raise
            except Exception as error:
                raise StorageError(
                    "Google sign-in failed or expired. Verify Desktop OAuth credentials, consent-screen test users, enabled APIs and localhost browser access."
                ) from error

    def _credentials(self):
        bound = self.bound_identity()
        if not bound:
            raise StorageError("Google is not signed in; run ekm auth google login.")
        credentials = self._load()
        try:
            refreshed = not credentials.valid
            if refreshed:
                if not credentials.refresh_token:
                    raise StorageError("Google session expired; sign in again.")
                credentials.refresh(Request())
                if not credentials.valid:
                    raise StorageError("Google did not renew this session; sign in again.")
            self._require_scopes(credentials)
            token_identity = (hashlib.sha256(credentials.token.encode()).hexdigest(), bound["sub"])
            if self._verified_token_identity != token_identity:
                self._validate_identity(self._userinfo(credentials.token))
                self._verified_token_identity = token_identity
            if refreshed:
                self._store(credentials)
            return credentials
        except StorageError:
            raise
        except Exception as error:
            raise StorageError(
                "Google session expired, was revoked, or could not refresh. Check connectivity and run ekm auth google login."
            ) from error

    @_suppress_oauth_diagnostics()
    def access_token(self) -> str:
        with self.lock:
            return self._credentials().token

    @_suppress_oauth_diagnostics()
    def status(self) -> dict:
        with self.lock:
            try:
                credentials = self._credentials()
                identity = self.bound_identity()
                expiry = credentials.expiry
                if expiry is not None:
                    expiry = (
                        expiry.replace(tzinfo=timezone.utc)
                        if expiry.tzinfo is None
                        else expiry.astimezone(timezone.utc)
                    )
                return {
                    "authenticated": True,
                    "provider": "google",
                    "account": identity,
                    "granted_scopes": list(credentials.granted_scopes or credentials.scopes or []),
                    "expires_at": expiry.isoformat() if expiry else None,
                    "token_expired": False,
                    "refresh_required": False,
                }
            except StorageError as error:
                return {
                    "authenticated": False,
                    "provider": "google",
                    "account": None,
                    "granted_scopes": [],
                    "expires_at": None,
                    "token_expired": None,
                    "refresh_required": True,
                    "message": str(error),
                }

    def logout(self) -> None:
        with self.lock:
            self._verified_token_identity = None
            # Clear cached credentials while retaining immutable account binding.
            try:
                self.persistence.save(
                    json.dumps(
                        {"client_id": self.client_id, "token_uri": TOKEN_URI, "signed_out": True}
                    )
                )
            except Exception as error:
                raise StorageError(
                    "Google encrypted session could not be cleared; retry sign-out."
                ) from error
