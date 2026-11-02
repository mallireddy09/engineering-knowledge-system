import httpx
import pytest

from ekm.microsoft.graph_client import GraphClient, GraphHTTPError
from ekm.providers.base import ConflictError, StorageError


def test_graph_validates_all_pagination_origins():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200, json={"value": [{"id": "1"}], "@odata.nextLink": "https://evil.example/leak"}
        )

    graph = GraphClient(lambda: "secret", httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(StorageError, match="origin"):
        graph.iter_collection("/me/drive/root/children")
    assert len(calls) == 1
    for bad in (
        "//evil.example/",
        "https://graph.microsoft.com.evil.example/v1.0/me",
        "http://graph.microsoft.com/v1.0/me",
    ):
        with pytest.raises(StorageError):
            graph.request("GET", bad)
    assert len(calls) == 1


def test_retry_reads_only_and_redact_error(monkeypatch):
    monkeypatch.setattr("ekm.microsoft.graph_client.time.sleep", lambda _: None)
    methods = []

    def handler(request):
        methods.append(request.method)
        return httpx.Response(503, json={"error": {"message": "confidential-source-and-token"}})

    graph = GraphClient(lambda: "secret", httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(GraphHTTPError) as error:
        graph.request("GET", "/me")
    assert methods == ["GET"] * 4
    assert "confidential" not in str(error.value)
    methods.clear()
    with pytest.raises(GraphHTTPError):
        graph.request("POST", "/me/drive/root/children", json={})
    assert methods == ["POST"]


def test_download_redirect_never_forwards_bearer_or_default_client_auth():
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.host == "graph.microsoft.com":
            return httpx.Response(
                302, headers={"Location": "https://files.1drv.com/download?capability=private"}
            )
        return httpx.Response(200, content=b"safe content")

    client = httpx.Client(
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer default"},
        auth=("user", "password"),
    )
    graph = GraphClient(lambda: "graph-token", client)
    assert graph.download("/me/drive/items/1/content") == b"safe content"
    assert seen[0].headers["authorization"] == "Bearer graph-token"
    assert "authorization" not in seen[1].headers


def test_download_blocks_untrusted_redirect():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": "https://127.0.0.1/private"})

    graph = GraphClient(lambda: "secret", httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(StorageError):
        graph.download("/me/drive/items/1/content")
    assert len(calls) == 1


def test_conflict_is_explicit():
    graph = GraphClient(
        lambda: "secret", httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(412)))
    )
    with pytest.raises(ConflictError):
        graph.request("PATCH", "/me/drive/items/1", json={})


def test_upload_session_rejects_untrusted_upload_url_before_sending_bytes():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"uploadUrl": "https://evil.example/capture"})

    graph = GraphClient(lambda: "token", httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(StorageError, match="unrecognized"):
        graph.upload_new("/me/drive/items/root", "record.json", b"confidential payload")
    assert len(calls) == 1
    assert b"confidential payload" not in calls[0].content


def test_throttle_respects_retry_after_only_for_reads(monkeypatch):
    delays = []
    monkeypatch.setattr("ekm.microsoft.graph_client.time.sleep", delays.append)
    calls = []

    def handler(request):
        calls.append(request.method)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return httpx.Response(200, json={"id": "ok"})

    graph = GraphClient(lambda: "token", httpx.Client(transport=httpx.MockTransport(handler)))
    assert graph.request("GET", "/me").json()["id"] == "ok"
    assert delays == [2]
    calls.clear()
    with pytest.raises(GraphHTTPError):
        graph.request("POST", "/me/drive/root/children", json={})
    assert calls == ["POST"]
