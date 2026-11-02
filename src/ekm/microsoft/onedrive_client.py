"""Path-contained file operations for an initialized EKM OneDrive folder."""

import hashlib
import os
from pathlib import Path
from urllib.parse import quote, urlsplit

from ekm.microsoft.graph_client import GraphClient
from ekm.providers.base import ConflictError, StorageError, require_approval

FOLDERS = frozenset({"journals", "dashboards", "reports", "manifests", "backups"})


def safe_name(name: str) -> str:
    if (
        not isinstance(name, str)
        or not name
        or name in {".", ".."}
        or any(ord(char) < 32 or char in '/\\:*?"<>|%' for char in name)
        or name != name.strip()
        or len(name) > 180
    ):
        raise StorageError(
            "Use a plain filename without paths, control characters or reserved characters."
        )
    return name


class OneDriveClient:
    def __init__(self, graph: GraphClient, root_id: str):
        if not root_id:
            raise StorageError("Initialize OneDrive storage before accessing files.")
        self.graph = graph
        self.root_id = root_id

    @property
    def root(self) -> str:
        return f"/me/drive/items/{quote(self.root_id, safe='')}"

    def _folder(self, folder: str) -> str:
        if folder not in FOLDERS:
            raise StorageError("File operations are restricted to EKM document folders.")
        path = f"{self.root}:/{folder}"
        metadata = self.graph.request("GET", path).json()
        if "remoteItem" in metadata:
            raise StorageError("EKM folder shortcuts to other locations are not permitted.")
        return path

    def _path(self, folder: str, name: str) -> str:
        return f"{self._folder(folder)}/{quote(safe_name(name), safe='')}"

    @staticmethod
    def _public(item: dict, content: bytes | None = None) -> dict:
        etag = item.get("eTag") or item.get("@odata.etag")
        if not item.get("id") or not etag:
            raise StorageError("OneDrive omitted the item identity or exact version.")
        url = item.get("webUrl", "")
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.query:
            url = ""
        return {
            "id": item["id"],
            "etag": etag,
            "name": item.get("name", ""),
            "url": url,
            "content_hash": hashlib.sha256(content).hexdigest() if content is not None else None,
        }

    def metadata(self, folder: str, name: str) -> dict:
        return self._public(self.graph.request("GET", self._path(folder, name)).json())

    def list(self, folder: str) -> list[dict]:
        return [
            self._public(item)
            for item in self.graph.iter_collection(f"{self._folder(folder)}:/children")
            if "file" in item
        ]

    def download(self, folder: str, name: str) -> tuple[bytes, dict]:
        before = self.metadata(folder, name)
        content = self.graph.download(f"/me/drive/items/{quote(before['id'], safe='')}/content")
        after = self.metadata(folder, name)
        if (before["id"], before["etag"]) != (after["id"], after["etag"]):
            raise ConflictError("File changed while downloading; fetch it again.")
        before["content_hash"] = hashlib.sha256(content).hexdigest()
        return content, before

    def upload(self, folder: str, name: str, content: bytes, *, approved: bool = False) -> dict:
        require_approval(approved)
        self._path(folder, name)
        parent = self.graph.request("GET", self._folder(folder)).json()
        item = self.graph.upload_new(
            f"/me/drive/items/{quote(parent['id'], safe='')}", name, content
        )
        return self._public(item, content)

    def update(
        self, folder: str, name: str, content: bytes, etag: str, *, approved: bool = False
    ) -> dict:
        require_approval(approved)
        if not etag or etag == "*":
            raise ConflictError("Updating a document requires its exact prior ETag.")
        item = self.graph.request(
            "PUT",
            f"{self._path(folder, name)}:/content",
            headers={"If-Match": etag, "Content-Type": "application/octet-stream"},
            content=content,
        ).json()
        return self._public(item, content)

    def delete(
        self,
        folder: str,
        name: str,
        etag: str,
        *,
        approved: bool = False,
        expected_id: str | None = None,
    ) -> None:
        require_approval(approved)
        if not etag or etag == "*":
            raise ConflictError("Deleting a file requires its exact ETag.")
        item = self.metadata(folder, name)
        if item["etag"] != etag or (expected_id is not None and item["id"] != expected_id):
            raise ConflictError("File changed; deletion was not performed.")
        self.graph.request(
            "DELETE", f"/me/drive/items/{quote(item['id'], safe='')}", headers={"If-Match": etag}
        )

    def download_to(self, folder: str, name: str, path: Path) -> dict:
        content, metadata = self.download(folder, name)
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            try:
                stream.write(content)
            except Exception:
                stream.close()
                path.unlink(missing_ok=True)
                raise
        return metadata
