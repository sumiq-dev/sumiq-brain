import json
import threading
import urllib.error
import urllib.request

import pytest

from aibrain.http_server import BrainHttpServer, is_loopback

TOKEN = "secret-token"


@pytest.fixture
def server(vault):
    httpd = BrainHttpServer(("127.0.0.1", 0), vault, TOKEN, [])
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()
    httpd.server_close()


def request(url, method="POST", body=None, token=TOKEN, session=None):
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if session:
        headers["Mcp-Session-Id"] = session
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
            return resp.status, dict(resp.headers), json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, dict(e.headers), json.loads(raw) if raw else None


def rpc(method, params=None, msg_id=1):
    msg = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if msg_id is not None:
        msg["id"] = msg_id
    return msg


def test_health_needs_no_token(server):
    status, _, body = request(server + "/health", method="GET", token=None)
    assert status == 200 and body["ok"] is True


def test_auth_required(server):
    status, headers, _ = request(server + "/mcp", body=rpc("ping"), token=None)
    assert status == 401 and headers["WWW-Authenticate"] == "Bearer"
    assert request(server + "/mcp", body=rpc("ping"), token="wrong")[0] == 401


def test_session_flow(server, vault):
    status, headers, body = request(server + "/mcp", body=rpc("initialize", {
        "protocolVersion": "2025-06-18", "clientInfo": {"name": "Laptop Claude"}}))
    assert status == 200 and body["result"]["serverInfo"]["name"] == "ai-brain"
    sid = headers["Mcp-Session-Id"]

    assert request(server + "/mcp", body=rpc("notifications/initialized", msg_id=None), session=sid)[0] == 202
    status, _, body = request(server + "/mcp", session=sid, body=rpc("tools/call", {
        "name": "write_note", "arguments": {"path": "topics/remote.md", "title": "Remote",
                                            "summary": "Written over HTTP.", "body": "hi"}}))
    assert body["result"]["isError"] is False
    assert vault.load("topics/remote.md").meta["author"] == "agent:laptop-claude"

    assert request(server + "/mcp", body=rpc("ping"), session="bogus")[0] == 404
    assert request(server + "/mcp", method="DELETE", session=sid)[0] == 204
    assert request(server + "/mcp", body=rpc("ping"), session=sid)[0] == 404


def test_get_stream_not_offered(server):
    assert request(server + "/mcp", method="GET")[0] == 405


def test_parse_error(server):
    req = urllib.request.Request(server + "/mcp", data=b"{nope", method="POST",
                                 headers={"Authorization": f"Bearer {TOKEN}"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 400


def test_loopback_detection():
    assert is_loopback("127.0.0.1") and is_loopback("localhost") and is_loopback("::1")
    assert not is_loopback("0.0.0.0") and not is_loopback("192.168.1.5")
