"""The add-on's HTTP side (odysseus_google/app.py): signed identities only,
the tool list with every write `asks_first`, tool calls, the page's API, the
Connect round trip through /oauth/callback, and the Ask before writing switch.
Google is a MockTransport; the core is never reached."""
import json
import os
import threading
import urllib.parse

import httpx
import pytest

from odysseus_mod import sign_identity
from odysseus_google import app as A, connection, products as P, store
from tests.test_products import FakeGoogle, REFRESH, ACCESS, URI  # noqa: F401

TOKEN = os.environ["ODYSSEUS_MOD_TOKEN"]
HOST = {"X-Forwarded-Host": "box.ts.net", "X-Forwarded-Proto": "https", "X-Forwarded-Prefix": "/mods/google/~frametok"}


@pytest.fixture
def server(tmp_path, monkeypatch):
    fake = FakeGoogle()
    monkeypatch.setenv("MOD_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(connection, "_transport", httpx.MockTransport(fake))
    connection._access.clear()
    A._apply_ask_first()
    srv = A.app.make_server("127.0.0.1", 0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def req(method, path, user="alice", **kw):
        h = dict(HOST, **sign_identity(TOKEN, "google", user)) if user else dict(HOST)
        h.update(kw.pop("headers", {}))
        return httpx.request(method, base + path, headers=h, timeout=10, follow_redirects=False, **kw)
    yield req, fake
    srv.shutdown()
    connection._access.clear()


def _setup_client(req):
    r = req("POST", "/api/client", json={"client_id": "123-abc.apps.googleusercontent.com",
                                         "client_secret": "client-secret"})
    assert r.status_code == 200, r.text
    return r.json()


def test_health_is_open_and_everything_else_needs_the_core(server):
    req, _ = server
    assert req("GET", "/health", user=None).status_code == 200
    assert req("GET", "/", user=None).status_code == 401
    assert req("GET", "/mod/tools", user=None).status_code == 401
    bad = dict(sign_identity("odm_wrong", "google", "alice"))
    assert req("GET", "/api/status", user=None, headers=bad).status_code == 401


def test_the_tool_list_marks_every_write_asks_first(server):
    req, _ = server
    tools = {t["function"]["name"]: t for t in req("GET", "/mod/tools", user="odysseus-core").json()["tools"]}
    assert set(tools) == set(P.TOOLS)
    for name, t in tools.items():
        x = t["x-odysseus"]
        assert x["asks_first"] is P.TOOLS[name]["mutates"], name
        assert x["read_only"] is (not P.TOOLS[name]["mutates"]), name
        assert not x["controls_devices"]
    assert "google doc" in tools["google_doc_create"]["x-odysseus"]["keywords"]
    assert "path" not in tools["google_drive_upload"]["function"]["parameters"]["properties"]
    assert len(tools) <= 24 and all(len(n) <= 40 for n in tools)


def test_page_status_connect_callback_and_tools(server):
    req, fake = server
    page = req("GET", "/").text
    assert "fetch(path" in page and "'api/status'" in page and 'href="connect?products=' in page
    st = req("GET", "/api/status").json()
    assert not st["configured"] and st["redirect_uri"] == URI and st["ask_first"] is True
    st = _setup_client(req)
    assert st["configured"] and st["client_id"].endswith(".apps.googleusercontent.com")
    assert "client-secret" not in json.dumps(st)
    # Connect: a 303 to Google's consent page with the fixed redirect URI.
    r = req("GET", "/connect?products=docs")
    assert r.status_code == 303 and r.headers["location"].startswith(connection.AUTH_URL)
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(r.headers["location"]).query))
    assert q["redirect_uri"] == URI and P.DOCS_SCOPE in q["scope"].split()
    fake.grant = [P.DOCS_SCOPE]
    # Another user cannot finish alice's connect.
    r = req("GET", f"/oauth/callback?state={q['state']}&code=c", user="bob")
    assert r.status_code == 400 and "another user" in r.text
    # bob's try used the state up; alice starts again and finishes.
    r = req("GET", "/connect?products=docs")
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(r.headers["location"]).query))
    r = req("GET", f"/oauth/callback?state={q['state']}&code=the-code")
    assert r.status_code == 200 and "Google connected" in r.text and REFRESH not in r.text
    st = req("GET", "/api/status").json()
    assert st["connected"] and [p["key"] for p in st["products"] if p["enabled"]] == ["docs"]
    assert REFRESH not in json.dumps(st) and ACCESS not in json.dumps(st)
    # A tool call for alice reaches Google; for bob it says how to connect.
    out = req("POST", "/mod/tools/google_doc_read", json={"document": "1DocDocDocDocDocDocDoc"}).json()
    assert "# Goals" in out["output"]
    out = req("POST", "/mod/tools/google_doc_read", user="bob", json={"document": "1DocDocDocDocDocDocDoc"}).json()
    assert "not connected" in out["error"]
    out = req("POST", "/mod/tools/google_doc_create", json={"title": "Plan", "content": "# A"}).json()
    assert "[New Google Doc: Plan](https://docs.google.com/document/d/" in out["output"]
    assert out["data"]["google_link"]["url"].startswith("https://docs.google.com/")
    out = req("POST", "/mod/tools/google_status", user="bob", json={}).json()
    assert "No Google product is connected" in out["output"] and "Settings > Google" in out["output"]
    # Disconnect forgets it here without revoking; revoke=True asks Google.
    st = req("POST", "/api/disconnect", json={"revoke": False}).json()
    assert not st["connected"] and not [c for c in fake.calls if c["url"].startswith(connection.REVOKE_URL)]


def test_disconnect_with_revoke_asks_google(server):
    req, fake = server
    _setup_client(req)
    r = req("GET", "/connect?products=tasks")
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(r.headers["location"]).query))
    fake.grant = [P.TASKS_SCOPE]
    assert req("GET", f"/oauth/callback?state={q['state']}&code=c").status_code == 200
    req("POST", "/api/disconnect", json={"revoke": True})
    assert [c for c in fake.calls if c["url"].startswith(connection.REVOKE_URL)]


def test_only_the_client_owner_changes_it_or_turns_asking_off(server):
    req, _ = server
    _setup_client(req)
    r = req("POST", "/api/client", user="bob", json={"client_id": "9-x.apps.googleusercontent.com", "client_secret": "s"})
    assert r.status_code == 403
    assert req("POST", "/api/ask_first", user="bob", json={"enabled": False}).status_code == 403
    assert req("POST", "/api/ask_first", json={"enabled": False}).json()["ask_first"] is False
    tools = {t["function"]["name"]: t for t in req("GET", "/mod/tools").json()["tools"]}
    assert not any(t["x-odysseus"]["asks_first"] for t in tools.values())
    # Anyone may turn it back on.
    assert req("POST", "/api/ask_first", user="bob", json={"enabled": True}).json()["ask_first"] is True
    tools = {t["function"]["name"]: t for t in req("GET", "/mod/tools").json()["tools"]}
    assert tools["google_sheet_write"]["x-odysseus"]["asks_first"] is True
    assert req("POST", "/api/client", json={"client_id": "nope", "client_secret": "s"}).status_code == 400


def test_redirect_uri_standalone_and_override(tmp_path, monkeypatch):
    monkeypatch.setenv("MOD_DATA_DIR", str(tmp_path / "data"))
    assert connection.redirect_uri({"Host": "127.0.0.1:8765"}, "") == "http://127.0.0.1:8765/oauth/callback"
    store.save_client("1-a.apps.googleusercontent.com", "s", "https://odysseus.example/mods/google/oauth/callback")
    assert connection.redirect_uri(HOST, "/mods/google/~x") == "https://odysseus.example/mods/google/oauth/callback"


def test_the_api_base_override_points_every_call_at_a_fake(monkeypatch):
    monkeypatch.setenv(connection.API_BASE_ENV, "http://127.0.0.1:5999/")
    assert connection.api_url("https://docs.googleapis.com/v1/documents/x?a=1") == \
        "http://127.0.0.1:5999/docs.googleapis.com/v1/documents/x?a=1"
    monkeypatch.delenv(connection.API_BASE_ENV)
    assert connection.api_url(connection.TOKEN_URL) == connection.TOKEN_URL


def test_manifest_has_a_tutorial_and_no_em_dashes():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    man = json.load(open(os.path.join(root, "odysseus-mod.json")))
    assert man["id"] == "google" and 3 <= len(man["tutorial"]["steps"]) <= 8
    assert man["permissions"] == []
    for dirpath, _, files in os.walk(root):
        if any(p in dirpath for p in (".git", "__pycache__", ".venv", "data")):
            continue
        for f in files:
            if f.endswith((".py", ".md", ".json")):
                assert chr(0x2014) not in open(os.path.join(dirpath, f), encoding="utf-8").read(), f
