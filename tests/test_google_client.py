import json

import httpx
import pytest

from ekm.google.client import DRIVE, GoogleAPIError, GoogleClient
from ekm.providers.base import ConflictError, NotFoundError, StorageError


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/drive/v3/files",
        "https://www.googleapis.com.evil.example/drive/v3/files",
        "http://www.googleapis.com/drive/v3/files",
        "https://www.googleapis.com:444/drive/v3/files",
        "https://user:password@www.googleapis.com/drive/v3/files",
        "https://www.googleapis.com/other/files",
        "https://docs.googleapis.com/v1/documents#private",
        "//www.googleapis.com/drive/v3/files",
    ],
)
def test_google_client_rejects_other_origins_before_token_request(url):
    client = GoogleClient(lambda: pytest.fail("token requested for invalid origin"))
    with pytest.raises(StorageError):
        client.request("GET", url)
    client.close()


def test_google_client_safe_pagination_and_no_default_auth():
    seen = []

    def handler(request):
        seen.append(request)
        assert request.headers["authorization"] == "Bearer correct"
        if "pageToken" not in request.url.params:
            return httpx.Response(200, json={"files": [{"id": "first"}], "nextPageToken": "next"})
        return httpx.Response(200, json={"files": [{"id": "second"}]})

    transport = httpx.Client(
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer wrong"},
        auth=("user", "secret"),
    )
    client = GoogleClient(lambda: "correct", transport)
    assert [f["id"] for f in client.iter_files("trashed = false")] == ["first", "second"]
    assert len(seen) == 2
    assert seen[0].url.params["spaces"] == "drive"


def test_google_retry_reads_but_never_mutations_or_raw_errors(monkeypatch):
    monkeypatch.setattr("ekm.google.client.time.sleep", lambda _: None)
    calls = []

    def handler(request):
        calls.append(request.method)
        return httpx.Response(503, json={"error": {"message": "private-token-response"}})

    client = GoogleClient(
        lambda: "secret-token", httpx.Client(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(GoogleAPIError) as error:
        client.request("GET", DRIVE + "/files")
    assert calls == ["GET"] * 4
    assert "private-token" not in str(error.value)
    calls.clear()
    with pytest.raises(GoogleAPIError):
        client.request("POST", DRIVE + "/files", json={})
    assert calls == ["POST"]


@pytest.mark.parametrize(
    "status,exception",
    [
        (404, NotFoundError),
        (409, ConflictError),
        (412, ConflictError),
        (403, GoogleAPIError),
        (302, StorageError),
    ],
)
def test_google_errors_and_redirects(status, exception):
    client = GoogleClient(
        lambda: "secret",
        httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(status, headers={"Location": "https://evil.example"})
            )
        ),
    )
    with pytest.raises(exception):
        client.request("GET", DRIVE + "/files")


@pytest.mark.parametrize(
    "result",
    [
        {"files": [], "incompleteSearch": True},
        {"files": "malformed"},
        {"files": [], "nextPageToken": "loop"},
    ],
)
def test_google_incomplete_listing_never_succeeds(result):
    client = GoogleClient(
        lambda: "secret",
        httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=result))),
    )
    with pytest.raises(StorageError):
        client.iter_files("trashed=false")


def test_multipart_upload_download_and_metadata():
    requests = []

    def handler(request):
        requests.append(request)
        if request.method == "POST":
            assert request.url.params["uploadType"] == "multipart"
            assert b'"id": "pregenerated"' in request.content
            assert b'{"safe": true}' in request.content
            assert request.headers["content-type"].startswith("multipart/related; boundary=ekm_")
            return httpx.Response(200, json={"id": "pregenerated", "version": "1"})
        if request.url.params.get("alt") == "media":
            return httpx.Response(200, content=b"record bytes")
        return httpx.Response(200, json={"id": "pregenerated", "version": "1"})

    client = GoogleClient(lambda: "secret", httpx.Client(transport=httpx.MockTransport(handler)))
    assert (
        client.upload_file({"id": "pregenerated"}, json.dumps({"safe": True}).encode())["id"]
        == "pregenerated"
    )
    assert client.download("pregenerated") == b"record bytes"
    assert client.file_metadata("pregenerated")["version"] == "1"
    with pytest.raises(StorageError):
        client.download("../outside")
    assert len(requests) == 3
