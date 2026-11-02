"""Graph-only bearer transport and explicitly unauthenticated file transfers."""

import ipaddress
import logging
import time
from collections.abc import Callable
from urllib.parse import urlsplit

import httpx

from ekm.providers.base import ConflictError, NotFoundError, StorageError

logger = logging.getLogger(__name__)


class GraphHTTPError(StorageError):
    def __init__(self, status_code: int):
        self.status_code = status_code
        hint = {
            401: "sign in again",
            403: "verify consent and site permissions",
            429: "retry later after throttling",
            507: "check storage quota",
        }.get(status_code, "retry or inspect service status")
        super().__init__(f"Microsoft Graph returned HTTP {status_code}; {hint}.")


class GraphClient:
    BASE = "https://graph.microsoft.com/v1.0"

    def __init__(self, token_provider: Callable[[], str], client: httpx.Client | None = None):
        self.token_provider = token_provider
        self.client = client or httpx.Client(timeout=30, follow_redirects=False)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    @classmethod
    def _url(cls, path: str) -> str:
        if path.startswith("/") and not path.startswith("//"):
            path = cls.BASE + path
        parsed = urlsplit(path)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "graph.microsoft.com"
            or parsed.port not in (None, 443)
            or parsed.username
            or parsed.password
            or parsed.fragment
            or not parsed.path.startswith("/v1.0/")
        ):
            raise StorageError("Refusing a Graph request outside the Microsoft Graph v1.0 origin.")
        return path

    @staticmethod
    def _check(response: httpx.Response) -> httpx.Response:
        if response.status_code == 404:
            raise NotFoundError("Remote item not found.")
        if response.status_code in (409, 412):
            raise ConflictError(
                "Remote version or identity conflict; fetch and reconcile explicitly."
            )
        if response.status_code >= 400:
            raise GraphHTTPError(response.status_code)
        return response

    def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        url = self._url(path)
        method = method.upper()
        headers = dict(kwargs.pop("headers", {}))
        headers["Authorization"] = "Bearer " + self.token_provider()
        # Construct Request directly so injected clients cannot add credentials or redirect policy.
        for attempt in range(4):
            request = httpx.Request(method, url, headers=headers, **kwargs)
            try:
                response = self.client.send(request, auth=None, follow_redirects=False)
            except httpx.TransportError as error:
                logger.warning(
                    "graph_transport_failed", extra={"graph_method": method, "attempt": attempt + 1}
                )
                if method in ("GET", "HEAD") and attempt < 3:
                    time.sleep(0.25 * 2**attempt)
                    continue
                raise StorageError(
                    "Microsoft transport failed; a write may have succeeded. Reconcile by record ID before retrying."
                ) from error
            logger.info(
                "graph_response",
                extra={
                    "graph_method": method,
                    "graph_status": response.status_code,
                    "attempt": attempt + 1,
                },
            )
            if (
                method in ("GET", "HEAD")
                and response.status_code in (429, 503, 504)
                and attempt < 3
            ):
                try:
                    delay = min(max(float(response.headers.get("Retry-After", 0)), 0), 30)
                except ValueError:
                    delay = 0
                time.sleep(delay or 0.25 * 2**attempt)
                continue
            return self._check(response)
        raise StorageError("Microsoft retry limit exceeded.")

    def iter_collection(self, path: str) -> list[dict]:
        values: list[dict] = []
        seen: set[str] = set()
        while path:
            url = self._url(path)
            if url in seen:
                raise StorageError("Microsoft returned a pagination loop.")
            seen.add(url)
            result = self.request("GET", url).json()
            values.extend(result["value"])
            path = result.get("@odata.nextLink", "")
        return values

    @staticmethod
    def _transfer_url(url: str) -> str:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        suffixes = (
            ".1drv.com",
            ".sharepoint.com",
            ".onedrive.com",
            ".onedrive.live.com",
            ".storage.live.com",
        )
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise StorageError("Refusing an IP-address file transfer destination.")
        if (
            parsed.scheme != "https"
            or parsed.port not in (None, 443)
            or parsed.username
            or parsed.password
            or parsed.fragment
            or not any(host.endswith(suffix) for suffix in suffixes)
        ):
            raise StorageError("Refusing an unrecognized Microsoft file transfer destination.")
        return url

    def _transfer(
        self, method: str, url: str, *, content: bytes | None = None, headers: dict | None = None
    ) -> httpx.Response:
        url = self._transfer_url(url)
        request = httpx.Request(method, url, content=content, headers=headers)
        try:
            return self._check(self.client.send(request, auth=None, follow_redirects=False))
        except httpx.TransportError as error:
            raise StorageError(
                "File transfer failed; reconcile remote state before retrying."
            ) from error

    def download(self, path: str) -> bytes:
        response = self.request("GET", path)
        for _ in range(4):
            if response.status_code not in (301, 302, 303, 307, 308):
                return response.content
            response = self._transfer("GET", response.headers.get("Location", ""))
        raise StorageError("File download redirected too many times.")

    def upload_new(self, parent_path: str, filename: str, content: bytes) -> dict:
        """Create atomically with conflictBehavior=fail; never replace existing names."""
        from urllib.parse import quote

        if not content:
            raise StorageError("Empty document uploads are unsupported.")
        if len(content) > 50 * 1024 * 1024:
            raise StorageError("Export exceeds the 50 MiB application upload limit.")
        path = f"{parent_path}:/{quote(filename, safe='')}:/createUploadSession"
        session = self.request(
            "POST",
            path,
            json={"item": {"@microsoft.graph.conflictBehavior": "fail", "name": filename}},
        ).json()
        upload_url = session["uploadUrl"]
        chunk_size = 10 * 320 * 1024
        response = None
        for start in range(0, len(content), chunk_size):
            chunk = content[start : start + chunk_size]
            end = start + len(chunk) - 1
            response = self._transfer(
                "PUT",
                upload_url,
                content=chunk,
                headers={"Content-Range": f"bytes {start}-{end}/{len(content)}"},
            )
        if response is None or response.status_code not in (200, 201):
            raise StorageError(
                "Upload is incomplete; reconcile the remote session before retrying."
            )
        return response.json()
