"""Delegated OAuth with encrypted caches and immutable profile identity binding."""

import json
import os
import tempfile
from pathlib import Path
from uuid import UUID

import msal
from filelock import FileLock
from msal_extensions import PersistedTokenCache, build_encrypted_persistence

from ekm.providers.base import StorageError

CONSUMER_TENANT = "9188040d-6c67-4c5b-b112-36a304b66dad"


class MicrosoftAuth:
    def __init__(
        self,
        profile_dir: Path,
        client_id: str,
        account_type: str,
        tenant_id: str | None = None,
        scopes: list[str] | None = None,
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
        self.app = msal.PublicClientApplication(client_id, authority=authority, token_cache=cache)

    def _binding(self) -> dict | None:
        try:
            return json.loads(self.binding_path.read_text())
        except FileNotFoundError:
            return None
        except (ValueError, OSError) as error:
            raise StorageError(
                "Profile identity binding cannot be read; repair the profile before sign-in."
            ) from error

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

    def login(self) -> dict:
        flow = self.app.initiate_device_flow(scopes=self.scopes)
        if "user_code" not in flow:
            raise StorageError(
                "Unable to start Microsoft device sign-in; verify public-client configuration and tenant policy."
            )
        # The device code message is intentionally user-visible; tokens and raw OAuth errors are not.
        print(flow.get("message", "Complete Microsoft sign-in in your browser."))
        result = self.app.acquire_token_by_device_flow(flow)
        if "access_token" not in result:
            raise StorageError(
                "Microsoft sign-in failed or expired; verify consent, tenant and device-flow policy."
            )
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
        return identity

    def access_token(self) -> str:
        with self._binding_lock:
            binding = self._binding()
            if not binding:
                raise StorageError("This profile is not signed in; run account login.")
            accounts = [
                account
                for account in self.app.get_accounts()
                if account.get("home_account_id") == binding.get("home_account_id")
                and account.get("realm", "").casefold() == str(self.tenant_id).casefold()
            ]
            if len(accounts) != 1:
                raise StorageError(
                    "No matching cached Microsoft identity; sign in to this profile again."
                )
            result = self.app.acquire_token_silent(self.scopes, account=accounts[0])
            if not result or "access_token" not in result:
                raise StorageError(
                    "Microsoft session expired or consent changed; run account login."
                )
            self._validate(accounts[0], result)
            return result["access_token"]

    def logout(self) -> None:
        with self._binding_lock:
            # Preserve binding: signing out must never re-purpose an existing company's cache.
            for account in self.app.get_accounts():
                self.app.remove_account(account)
