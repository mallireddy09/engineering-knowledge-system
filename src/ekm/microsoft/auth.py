"""Delegated OAuth with encrypted caches and immutable profile identity binding."""

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

import msal
from filelock import FileLock
from msal_extensions import PersistedTokenCache, build_encrypted_persistence

from ekm.providers.base import StorageError
from ekm.security import suppress_oauth_diagnostics

_OAUTH_LOGGERS = ("msal", "msal_extensions")

CONSUMER_TENANT = "9188040d-6c67-4c5b-b112-36a304b66dad"


class MicrosoftAuth:
    @suppress_oauth_diagnostics(_OAUTH_LOGGERS, "Microsoft")
    def __init__(
        self,
        profile_dir: Path,
        client_id: str,
        account_type: str,
        tenant_id: str | None = None,
        scopes: list[str] | None = None,
        redirect_uri: str = "http://localhost",
    ):
        if account_type not in ("personal", "company"):
            raise StorageError("Microsoft account type must be personal or company.")
        if not client_id:
            raise StorageError(
                "Register a Microsoft public-client application and configure its client ID."
            )
        try:
            UUID(client_id)
            if account_type == "company":
                tenant_id = str(UUID(tenant_id or ""))
        except ValueError as error:
            raise StorageError(
                "Application client ID and company tenant ID must be UUIDs."
            ) from error
        self.profile_dir = Path(profile_dir)
        self.redirect_port = self._redirect_port(redirect_uri)
        self.account_type = account_type
        self.tenant_id = CONSUMER_TENANT if account_type == "personal" else tenant_id
        self.scopes = scopes or (
            ["Files.ReadWrite.AppFolder"] if account_type == "personal" else ["Sites.Selected"]
        )
        self.profile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.binding_path = self.profile_dir / "identity.json"
        self._binding_lock = FileLock(str(self.profile_dir / ".identity.lock"))
        try:
            persistence = build_encrypted_persistence(str(self.profile_dir / "token-cache.bin"))
            if not persistence.is_encrypted:
                raise StorageError("Encrypted Microsoft token storage is unavailable.")
            cache = PersistedTokenCache(persistence)
        except Exception as error:
            raise StorageError(
                "Encrypted token storage is unavailable. Configure the OS keychain or Linux libsecret; plaintext fallback is disabled."
            ) from error
        authority = "https://login.microsoftonline.com/" + (
            "consumers" if account_type == "personal" else str(tenant_id)
        )
        try:
            self.app = msal.PublicClientApplication(
                client_id, authority=authority, token_cache=cache
            )
        except (ValueError, OSError, RuntimeError) as error:
            raise StorageError(
                "Unable to initialize Microsoft sign-in. Verify the existing application ID, "
                "supported personal account types, and network connectivity."
            ) from error

    @staticmethod
    def _redirect_port(uri: str) -> int | None:
        try:
            parsed = urlsplit(uri)
            port = parsed.port
        except ValueError as error:
            raise StorageError(
                "Use the desktop redirect URI http://localhost with an optional port."
            ) from error
        if (
            parsed.scheme != "http"
            or parsed.hostname != "localhost"
            or parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
            or (port is not None and not 1 <= port <= 65535)
            or uri.endswith(":")
        ):
            raise StorageError(
                "Only the desktop loopback redirect http://localhost[:port] is supported."
            )
        return port

    @staticmethod
    def _failure(result: dict | None, *, interactive: bool = False) -> StorageError:
        codes = {str(code) for code in (result or {}).get("error_codes", [])}
        if codes & {"50011", "500113"}:
            message = (
                "The existing application needs a Mobile and desktop applications platform "
                "with redirect URI http://localhost in Microsoft Entra Authentication settings."
            )
        elif codes & {"50194", "50020", "500200", "500202", "700016"}:
            message = (
                "Verify the existing application ID and its supported account types. "
                "Enable personal Microsoft accounts in Microsoft Entra; a new app is not required."
            )
        elif codes & {"7000218", "7000215"}:
            message = (
                "Configure this existing application as a public desktop client. "
                "Do not create a client secret; check the Mobile and desktop platform settings."
            )
        elif not interactive:
            message = (
                "Microsoft session expired, was revoked, or requires consent. "
                "Run ekm auth login to sign in to the same account again."
            )
        else:
            message = (
                "Microsoft sign-in failed, was cancelled, or expired. Verify personal-account "
                "support, delegated permissions, and the desktop redirect configuration."
            )
        return StorageError(message)

    def _binding(self) -> dict | None:
        try:
            binding = json.loads(self.binding_path.read_text())
            required = ("home_account_id", "tenant_id", "account_type")
            if not isinstance(binding, dict) or any(
                not isinstance(binding.get(key), str) or not binding[key].strip()
                for key in required
            ):
                raise ValueError("invalid identity metadata")
            if (
                binding["tenant_id"].casefold() != str(self.tenant_id).casefold()
                or binding["account_type"] != self.account_type
            ):
                raise ValueError("identity metadata does not match profile")
            return {key: binding[key] for key in required}
        except FileNotFoundError:
            return None
        except (ValueError, OSError) as error:
            raise StorageError(
                "Profile identity binding cannot be read; repair the profile before sign-in."
            ) from error

    def bound_identity(self) -> dict | None:
        """Return non-secret immutable identity metadata without acquiring a token."""
        with self._binding_lock:
            binding = self._binding()
            if binding is None:
                return None
            if (
                binding.get("tenant_id", "").casefold() != str(self.tenant_id).casefold()
                or binding.get("account_type") != self.account_type
                or not binding.get("home_account_id")
            ):
                raise StorageError(
                    "Profile identity binding does not match the configured account type."
                )
            return {key: binding[key] for key in ("home_account_id", "tenant_id", "account_type")}

    def _validate(self, account: dict, result: dict) -> dict:
        claims = result.get("id_token_claims", {})
        tenant = claims.get("tid") or account.get("realm")
        if not tenant or str(tenant).casefold() != str(self.tenant_id).casefold():
            raise StorageError(
                "Authenticated tenant does not match this profile; no knowledge was accessed."
            )
        home_id = account.get("home_account_id")
        if not home_id:
            raise StorageError("Microsoft omitted the account identity; sign-in was rejected.")
        identity = {
            "home_account_id": home_id,
            "tenant_id": str(tenant).lower(),
            "account_type": self.account_type,
        }
        bound = self._binding()
        if bound is not None and bound != identity:
            raise StorageError(
                "This profile is bound to another account. Create a separate profile instead."
            )
        return identity

    def _write_binding(self, identity: dict) -> None:
        fd, name = tempfile.mkstemp(dir=self.profile_dir, prefix=".identity-")
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(identity, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.binding_path)
        finally:
            Path(name).unlink(missing_ok=True)

    @suppress_oauth_diagnostics(_OAUTH_LOGGERS, "Microsoft")
    def login(self, flow: str = "device") -> dict:
        if flow not in {"device", "interactive"}:
            raise StorageError("Choose interactive browser sign-in or device sign-in.")
        try:
            if flow == "interactive":
                # MSAL owns the loopback listener, state verification, PKCE, and code exchange.
                result = self.app.acquire_token_interactive(
                    scopes=self.scopes,
                    timeout=180,
                    port=self.redirect_port,
                    prompt="select_account",
                )
            else:
                device_flow = self.app.initiate_device_flow(scopes=self.scopes)
                if "user_code" not in device_flow:
                    raise self._failure(device_flow, interactive=True)
                # Device instructions intentionally contain an ephemeral user code, never a token.
                # Build the message from known fields, not a potentially sensitive error body.
                print(
                    "Open https://microsoft.com/devicelogin and enter code "
                    + str(device_flow["user_code"])
                    + " to sign in."
                )
                result = self.app.acquire_token_by_device_flow(device_flow)
        except StorageError:
            raise
        except (ValueError, OSError, RuntimeError) as error:
            raise StorageError(
                "Microsoft sign-in could not complete. Verify network access and configure "
                "http://localhost as a desktop redirect for browser sign-in."
            ) from error
        if not result or "access_token" not in result:
            raise self._failure(result, interactive=True)
        claims = result.get("id_token_claims", {})
        local_id = claims.get("oid") or claims.get("sub")
        accounts = [
            account
            for account in self.app.get_accounts()
            if account.get("realm", "").casefold() == str(self.tenant_id).casefold()
            and (not local_id or account.get("local_account_id") == local_id)
        ]
        if len(accounts) != 1:
            raise StorageError(
                "Cannot uniquely identify the signed-in account; use a separate profile."
            )
        with self._binding_lock:
            identity = self._validate(accounts[0], result)
            self._write_binding(identity)
        return {**identity, **self._session_info(accounts[0], result)}

    def _bound_account(self) -> dict:
        binding = self._binding()
        if not binding:
            raise StorageError("This profile is not signed in; run ekm auth login.")
        accounts = [
            account
            for account in self.app.get_accounts()
            if account.get("home_account_id") == binding.get("home_account_id")
            and account.get("realm", "").casefold() == str(self.tenant_id).casefold()
        ]
        if len(accounts) != 1:
            raise StorageError("No matching cached Microsoft identity; run ekm auth login again.")
        return accounts[0]

    def _silent(self, account: dict) -> dict:
        try:
            acquire = getattr(self.app, "acquire_token_silent_with_error", None)
            if acquire is None:
                acquire = self.app.acquire_token_silent
            result = acquire(self.scopes, account=account)
        except (ValueError, OSError, RuntimeError) as error:
            raise StorageError(
                "Unable to refresh Microsoft authentication. Check network access and sign in again if needed."
            ) from error
        if not result or "access_token" not in result:
            raise self._failure(result)
        self._validate(account, result)
        if self._session_info(account, result)["token_expired"]:
            raise self._failure(None)
        return result

    @staticmethod
    def _session_info(account: dict, result: dict) -> dict:
        now = datetime.now(timezone.utc)
        expires_at = None
        try:
            if result.get("expires_on") is not None:
                expires_at = datetime.fromtimestamp(float(result["expires_on"]), timezone.utc)
            elif result.get("expires_in") is not None:
                expires_at = now + timedelta(seconds=float(result["expires_in"]))
        except (ValueError, TypeError, OverflowError, OSError):
            pass
        claims = result.get("id_token_claims", {})
        scopes = result.get("scope", "")
        granted = scopes.split() if isinstance(scopes, str) else []
        return {
            "account": {
                "username": account.get("username") or claims.get("preferred_username"),
                "name": claims.get("name") or account.get("name"),
                "home_account_id": account.get("home_account_id"),
                "tenant_id": claims.get("tid") or account.get("realm"),
            },
            "granted_scopes": granted,
            "expires_at": expires_at.isoformat() if expires_at else None,
            "token_expired": expires_at <= now if expires_at else None,
        }

    @suppress_oauth_diagnostics(_OAUTH_LOGGERS, "Microsoft")
    def status(self) -> dict:
        """Inspect or silently refresh this bound session; never open a sign-in window."""
        state = {
            "authenticated": False,
            "account": None,
            "account_type": self.account_type,
            "granted_scopes": [],
            "expires_at": None,
            "token_expired": None,
            "refresh_required": True,
        }
        with self._binding_lock:
            try:
                account = self._bound_account()
                result = self._silent(account)
            except StorageError as error:
                return {**state, "message": str(error)}
            return {
                **state,
                **self._session_info(account, result),
                "authenticated": True,
                "refresh_required": False,
                "message": "Microsoft session is available.",
            }

    @suppress_oauth_diagnostics(_OAUTH_LOGGERS, "Microsoft")
    def access_token(self) -> str:
        with self._binding_lock:
            account = self._bound_account()
            result = self._silent(account)
            return result["access_token"]

    @suppress_oauth_diagnostics(_OAUTH_LOGGERS, "Microsoft")
    def logout(self) -> None:
        with self._binding_lock:
            # Preserve binding: signing out must never re-purpose an existing company's cache.
            try:
                for account in self.app.get_accounts():
                    self.app.remove_account(account)
            except (ValueError, OSError, RuntimeError) as error:
                raise StorageError(
                    "Unable to clear the encrypted Microsoft session; retry sign-out."
                ) from error
