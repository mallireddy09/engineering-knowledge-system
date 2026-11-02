"""Google-only bearer HTTP transport with safe pagination and upload helpers."""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from urllib.parse import quote, urlsplit
from uuid import uuid4

import httpx

from ekm.providers.base import ConflictError, NotFoundError, StorageError

logger = logging.getLogger(__name__)
DRIVE = "https://www.googleapis.com/drive/v3"
METADATA_FIELDS = "id,name,mimeType,parents,appProperties,version,trashed,md5Checksum"


class GoogleAPIError(StorageError):
    def __init__(self, status_code: int):
        self.status_code = status_code
        hint = {
            401: "sign in to Google again",
            403: "check enabled APIs, scopes and file access",
            429: "retry after throttling",
        }.get(status_code, "inspect configuration or retry safely")
        super().__init__(f"Google API returned HTTP {status_code}; {hint}.")


def safe_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", value):
        raise StorageError("Invalid Google file identifier.")
    return value


class GoogleClient:
    def __init__(self, token_provider: Callable[[], str], client: httpx.Client | None = None):
        self.token_provider = token_provider
        self.client = client or httpx.Client(timeout=30, follow_redirects=False)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    @staticmethod
    def _url(path: str) -> str:
        parsed = urlsplit(path)
        prefixes = {
            "www.googleapis.com": ("/drive/v3/", "/upload/drive/v3/"),
            "docs.googleapis.com": ("/v1/",),
            "sheets.googleapis.com": ("/v4/",),
        }
        try:
            valid_port = parsed.port in {None, 443}
        except ValueError:
            valid_port = False
        if (
            parsed.scheme != "https"
            or not valid_port
            or parsed.username
            or parsed.password
            or parsed.fragment
            or not any(parsed.path.startswith(p) for p in prefixes.get(parsed.hostname, ()))
        ):
            raise StorageError("Refusing a request outside the supported Google API origins.")
        return path

    def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        path = self._url(path)
        method = method.upper()
        headers = dict(kwargs.pop("headers", {}))
        # Never inherit a caller's Authorization casing or a client's default credentials.
        headers = {k: v for k, v in headers.items() if k.lower() != "authorization"}
        headers["Authorization"] = "Bearer " + self.token_provider()
        for attempt in range(4):
            request = httpx.Request(method, path, headers=headers, **kwargs)
            try:
                response = self.client.send(request, auth=None, follow_redirects=False)
            except httpx.TransportError as error:
                if method in {"GET", "HEAD"} and attempt < 3:
                    time.sleep(0.25 * 2**attempt)
                    continue
                raise StorageError(
                    "Google transport failed; reconcile operation identity before retrying a write."
                ) from error
            logger.info(
                "google_response",
                extra={
                    "google_method": method,
                    "google_status": response.status_code,
                    "attempt": attempt + 1,
                },
            )
            if (
                method in {"GET", "HEAD"}
                and response.status_code in {429, 500, 502, 503, 504}
                and attempt < 3
            ):
                try:
                    delay = min(max(float(response.headers.get("Retry-After", 0)), 0), 30)
                except ValueError:
                    delay = 0
                time.sleep(delay or 0.25 * 2**attempt)
                continue
            if response.status_code == 404:
                raise NotFoundError("Google file not found.")
            if response.status_code in {409, 412}:
                raise ConflictError("Google file version or operation identity conflict.")
            if response.status_code >= 400:
                raise GoogleAPIError(response.status_code)
            if 300 <= response.status_code < 400:
                raise StorageError(
                    "Google API redirected unexpectedly; no bearer credentials were forwarded."
                )
            return response
        raise StorageError("Google retry limit exceeded.")

    def iter_files(self, query: str, fields: str = METADATA_FIELDS) -> list[dict]:
        items, page, seen = [], None, set()
        while True:
            params = {
                "q": query,
                "pageSize": 1000,
                "spaces": "drive",
                "fields": f"nextPageToken,incompleteSearch,files({fields})",
            }
            if page:
                params["pageToken"] = page
            result = self.request("GET", DRIVE + "/files", params=params).json()
            if result.get("incompleteSearch"):
                raise StorageError(
                    "Google returned an incomplete file listing; no cache was replaced."
                )
            if not isinstance(result.get("files"), list):
                raise StorageError("Google returned an invalid file listing.")
            items.extend(result["files"])
            page = result.get("nextPageToken")
            if not page:
                return items
            if not isinstance(page, str) or page in seen:
                raise StorageError("Google returned a pagination loop.")
            seen.add(page)

    def file_metadata(self, file_id: str) -> dict:
        return self.request(
            "GET", DRIVE + "/files/" + quote(safe_id(file_id)), params={"fields": METADATA_FIELDS}
        ).json()

    def download(self, file_id: str) -> bytes:
        return self.request(
            "GET", DRIVE + "/files/" + quote(safe_id(file_id)), params={"alt": "media"}
        ).content

    def upload_file(
        self, metadata: dict, content: bytes, mime_type: str = "application/json"
    ) -> dict:
        if len(content) > 50 * 1024 * 1024:
            raise StorageError("Google upload exceeds the 50 MiB EKM limit.")
        boundary = "ekm_" + uuid4().hex
        body = (
            f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode()
            + json.dumps(metadata, ensure_ascii=False).encode()
            + f"\r\n--{boundary}\r\nContent-Type: {mime_type}\r\n\r\n".encode()
            + content
            + f"\r\n--{boundary}--\r\n".encode()
        )
        return self.request(
            "POST",
            "https://www.googleapis.com/upload/drive/v3/files",
            params={"uploadType": "multipart", "fields": METADATA_FIELDS},
            headers={"Content-Type": f"multipart/related; boundary={boundary}"},
            content=body,
        ).json()
