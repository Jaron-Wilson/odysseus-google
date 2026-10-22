"""The Google add-on's connection and products (odysseus_google/): incremental
consent per product, token refresh, each tool's request shape and trimmed
output, owner isolation. Ported from Odysseus's tests/test_google_workspace.py
when the products moved out of the core.

Google is never reached: every call goes to an httpx.MockTransport that
plays the token endpoint and each API. No real tokens are used.
"""

import asyncio
import base64
import json
import os
import urllib.parse
from pathlib import Path

import httpx
import pytest
from odysseus_google import common, connection, docs, products as P, sheets, store, tools

ROOT = Path(__file__).resolve().parents[1]
REFRESH = "1//refresh-token-never-shown"
REFRESH2 = "1//second-refresh-token"
ACCESS = "ya29.access-token"
DOC_ID = "1DocDocDocDocDocDocDoc"
SHEET_ID = "1SheetSheetSheetSheet"
DECK_ID = "1DeckDeckDeckDeckDeck"
FILE_ID = "1FileFileFileFileFile"


def _id_token(email):
    body = base64.urlsafe_b64encode(json.dumps({"email": email, "sub": "1"}).encode()).decode().rstrip("=")
    return f"x.{body}.y"


class FakeGoogle:
    """The token endpoint and the APIs, with every call recorded."""

    def __init__(self):
        self.calls = []
        self.grant = []                 # scopes the next code exchange grants
        self.email = "jaron@example.com"
        self.refresh_token = REFRESH
        self.refresh = "ok"             # "ok" or "invalid_grant"
        self.refreshes = 0
        self.expire_once = False        # answer the next API call 401
        self.doc_text = "Hello"
        self.api_status = 200
        self.returned = None            # the token response's `scope`, when it differs from grant
        self.tokeninfo = None           # scopes tokeninfo reports (None: same as grant), or "error"
        self.aud = "123-abc.apps.googleusercontent.com"

    def last(self, method, part):
        for c in reversed(self.calls):
            if c["method"] == method and part in c["url"]:
                return c
        raise AssertionError(f"no {method} {part}: {[(c['method'], c['url']) for c in self.calls]}")

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        body = request.content.decode("utf-8", "replace") if request.content else ""
        self.calls.append({"method": request.method, "url": url, "body": body,
                           "auth": request.headers.get("authorization", ""),
                           "ctype": request.headers.get("content-type", "")})
        if url.startswith(connection.TOKEN_URL) and not url.startswith(connection.TOKENINFO_URL):
            form = dict(urllib.parse.parse_qsl(body))
            if form.get("grant_type") == "authorization_code":
                return httpx.Response(200, json={
                    "access_token": ACCESS, "expires_in": 3600, "refresh_token": self.refresh_token,
                    "scope": " ".join(["openid", "https://www.googleapis.com/auth/userinfo.email"] +
                                      (self.grant if self.returned is None else self.returned)),
                    "id_token": _id_token(self.email)})
            self.refreshes += 1
            if self.refresh == "invalid_grant":
                return httpx.Response(400, json={"error": "invalid_grant"})
            assert form.get("refresh_token") in (REFRESH, REFRESH2)
            return httpx.Response(200, json={"access_token": ACCESS, "expires_in": 3600})
        if url.startswith(connection.REVOKE_URL):
            return httpx.Response(200)
        if url.startswith(connection.TOKENINFO_URL):
            assert request.method == "GET" and "authorization" not in request.headers
            assert dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query)).get("access_token") == ACCESS
            if self.tokeninfo == "error":
                return httpx.Response(503, json={"error": "backendError"})
            held = self.grant if self.tokeninfo is None else self.tokeninfo
            return httpx.Response(200, json={
                "aud": self.aud, "azp": self.aud, "expires_in": "3599", "email": self.email,
                "scope": " ".join(["openid", "https://www.googleapis.com/auth/userinfo.email"] + held)})
        assert request.headers.get("authorization") == f"Bearer {ACCESS}"
        if self.expire_once:
            self.expire_once = False
            return httpx.Response(401, json={"error": {"status": "UNAUTHENTICATED"}})
        if self.api_status != 200:
            return httpx.Response(self.api_status, json={"error": {
                "status": "PERMISSION_DENIED", "message": "x", "details": [{"reason": "SERVICE_DISABLED"}]}})
        u = urllib.parse.urlsplit(url)
        path = u.path
        q = dict(urllib.parse.parse_qsl(u.query))
        # Drive
        if u.netloc == "www.googleapis.com" and path.startswith("/upload/drive/v3/files"):
            return httpx.Response(200, json={"id": FILE_ID, "name": "notes", "mimeType": P_GDOC,
                                             "webViewLink": f"https://docs.google.com/document/d/{FILE_ID}/edit"})
        if path == "/drive/v3/files" and request.method == "GET":
            return httpx.Response(200, json={"files": [
                {"id": DOC_ID, "name": "Taxes 2026", "mimeType": P_GDOC, "modifiedTime": "2026-10-01T10:00:00Z",
                 "webViewLink": f"https://docs.google.com/document/d/{DOC_ID}/edit"}],
                "nextPageToken": "NEXT"})
        if path == "/drive/v3/files" and request.method == "POST":
            meta = json.loads(body)
            return httpx.Response(200, json={"id": "1FolderFolderFolder", "name": meta["name"], "mimeType": meta["mimeType"]})
        if path == f"/drive/v3/files/{DOC_ID}" and "alt" not in q:
            return httpx.Response(200, json={"id": DOC_ID, "name": "Taxes 2026", "mimeType": P_GDOC,
                                             "modifiedTime": "2026-10-01T10:00:00Z",
                                             "webViewLink": f"https://docs.google.com/document/d/{DOC_ID}/edit"})
        if path == f"/drive/v3/files/{DOC_ID}/export":
            return httpx.Response(200, text=self.doc_text)
        if path.startswith("/drive/v3/files/"):
            return httpx.Response(404, json={"error": {"status": "NOT_FOUND", "message": "File not found"}})
        # Docs
        if u.netloc == "docs.googleapis.com":
            if path == "/v1/documents" and request.method == "POST":
                return httpx.Response(200, json={"documentId": DOC_ID, "title": json.loads(body)["title"]})
            if path.endswith(":batchUpdate"):
                reqs = json.loads(body)["requests"]
                if "replaceAllText" in reqs[0]:
                    return httpx.Response(200, json={"replies": [{"replaceAllText": {"occurrencesChanged": 2}}]})
                return httpx.Response(200, json={"replies": []})
            return httpx.Response(200, json=SAMPLE_DOC)
        # Sheets
        if u.netloc == "sheets.googleapis.com":
            if path == "/v4/spreadsheets" and request.method == "POST":
                return httpx.Response(200, json={"spreadsheetId": SHEET_ID, "spreadsheetUrl":
                                                 f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit",
                                                 "sheets": [{"properties": {"title": "Budget"}}]})
            if "/values/" in path and request.method == "GET":
                rows = [[f"r{i}", str(i), "x" * 400] for i in range(250)]
                return httpx.Response(200, json={"range": "Sheet1!A1:C250", "values": rows})
            if path.endswith(":append"):
                return httpx.Response(200, json={"updates": {"updatedRows": 1, "updatedRange": "Sheet1!A251:B251"}})
            if "/values/" in path and request.method == "PUT":
                return httpx.Response(200, json={"updatedCells": 4, "updatedRange": "Sheet1!A1:B2"})
            return httpx.Response(200, json={"spreadsheetId": SHEET_ID, "properties": {"title": "Budget"},
                                             "sheets": [{"properties": {"title": "Sheet1", "gridProperties":
                                                                        {"rowCount": 1000, "columnCount": 26}}},
                                                        {"properties": {"title": "Notes", "gridProperties":
                                                                        {"rowCount": 10, "columnCount": 2}}}]})
        # Slides
        if u.netloc == "slides.googleapis.com":
            if path == "/v1/presentations" and request.method == "POST":
                return httpx.Response(200, json={"presentationId": DECK_ID, "slides": [{"objectId": "p"}]})
            if path.endswith(":batchUpdate"):
                return httpx.Response(200, json={"replies": []})
            return httpx.Response(200, json={"title": "Pitch", "slides": [
                {"pageElements": [{"shape": {"text": {"textElements": [{"textRun": {"content": "Why now\n"}}]}}}],
                 "slideProperties": {"notesPage": {"pageElements": [
                     {"shape": {"text": {"textElements": [{"textRun": {"content": "Say it slowly"}}]}}}]}}}]})
        # Tasks
        if u.netloc == "tasks.googleapis.com":
            if request.method == "POST":
                return httpx.Response(200, json={"id": "t9", **json.loads(body)})
            if path.endswith("/users/@me/lists"):
                return httpx.Response(200, json={"items": [{"id": "MDE", "title": "My Tasks"}]})
            return httpx.Response(200, json={"items": [{"id": "t1", "title": "Pay rent", "status": "needsAction",
                                                        "due": "2026-10-20T00:00:00.000Z"}]})
        # People
        if u.netloc == "people.googleapis.com":
            return httpx.Response(200, json={"results": [{"person": {
                "names": [{"displayName": "Amy Pond"}], "emailAddresses": [{"value": "amy@example.com"}],
                "phoneNumbers": [{"value": "+1 555 0100"}], "organizations": [{"name": "TARDIS", "title": "CTO"}]}}]})
        return httpx.Response(404, json={"error": {"status": "NOT_FOUND"}})


P_GDOC = "application/vnd.google-apps.document"

SAMPLE_DOC = {
    "title": "Plan",
    "lists": {"L1": {"listProperties": {"nestingLevels": [{"glyphSymbol": "●"}]}},
              "L2": {"listProperties": {"nestingLevels": [{"glyphType": "DECIMAL"}]}}},
    "body": {"content": [
        {"endIndex": 1, "sectionBreak": {}},
        {"endIndex": 7, "paragraph": {"paragraphStyle": {"namedStyleType": "HEADING_1"},
                                      "elements": [{"textRun": {"content": "Goals\n"}}]}},
        {"endIndex": 12, "paragraph": {"bullet": {"listId": "L1"}, "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                                       "elements": [{"textRun": {"content": "Ship\n"}}]}},
        {"endIndex": 17, "paragraph": {"bullet": {"listId": "L2"}, "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                                       "elements": [{"textRun": {"content": "Test\n"}}]}},
        {"endIndex": 40, "table": {"tableRows": [
            {"tableCells": [{"content": [{"paragraph": {"elements": [{"textRun": {"content": "Name\n"}}]}}]},
                            {"content": [{"paragraph": {"elements": [{"textRun": {"content": "Due\n"}}]}}]}]},
            {"tableCells": [{"content": [{"paragraph": {"elements": [{"textRun": {"content": "Docs\n"}}]}}]},
                            {"content": [{"paragraph": {"elements": [{"textRun": {"content": "Fri\n"}}]}}]}]}]}},
        {"endIndex": 50, "paragraph": {"elements": [{"textRun": {"content": "The end.\n"}}]}},
    ]},
}

ALL = [s for p in P.PRODUCTS.values() for s in p["scopes"] + p.get("extra", [])]


URI = "https://box.ts.net/mods/google/oauth/callback"


@pytest.fixture
def google(tmp_path, monkeypatch):
    fake = FakeGoogle()
    monkeypatch.setenv("MOD_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(connection, "_transport", httpx.MockTransport(fake))
    monkeypatch.delenv(connection.API_BASE_ENV, raising=False)
    store.save_client("123-abc.apps.googleusercontent.com", "client-secret", by="alice")
    connection._access.clear()
    yield fake
    connection._access.clear()


def _connect(fake, products, user="alice", grant=None):
    """Run the consent round trip for `products`; Google grants `grant`
    (default: everything asked for plus what it granted before)."""
    url = connection.connect_url(user, products, URI)
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    asked = q["scope"].split()
    fake.grant = sorted(set(fake.grant) | set(s for s in asked if s.startswith("https://"))) if grant is None else grant
    entry = connection.pop_state(q["state"])
    ok, msg = asyncio.run(connection.finish_connect(user, "the-code", entry))
    return ok, msg, q


def run(tool, args, user="alice"):
    """A tool call, as the mod answers it, with the old {exit_code, ...} shape
    for the assertions: data (google_link, files...) merged in."""
    res = asyncio.run(tools.run_tool(tool, args, owner=user))
    if "error" in res:
        return {"exit_code": 1, "error": res["error"]}
    return {"exit_code": 0, "output": res["output"], **(res.get("data") or {})}


# ── Connecting, one product at a time ────────────────────────────────────

def test_connect_asks_for_just_that_product_incrementally(google):
    url = connection.connect_url("alice", ["docs"], URI)
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    assert q["scope"].split() == ["openid", "email", P.DOCS_SCOPE]
    assert q["include_granted_scopes"] == "true" and q["access_type"] == "offline" and q["prompt"] == "consent"
    assert q["redirect_uri"] == URI
    entry = connection.pop_state(q["state"])
    assert entry["products"] == ["docs"] and entry["user"] == "alice" and entry["code_verifier"]
    assert q["code_challenge_method"] == "S256"
    # Read-all Drive brings Drive along; nonsense is dropped.
    assert connection.clean_products("drive_all,bogus") == ["drive", "drive_all"]
    with pytest.raises(connection.GoogleError):
        connection.connect_url("alice", ["bogus"], URI)


def test_scope_upgrade_adds_a_product_and_keeps_the_others(google, tmp_path):
    ok, msg, _ = _connect(google, ["drive"])
    assert ok and "Google Drive" in msg and "jaron@example.com" in msg
    assert connection.enabled_products("alice") == ["drive"]
    google.refresh_token = REFRESH2
    ok, msg, q = _connect(google, ["docs"])
    assert ok and "Google Docs" in msg
    # The new one plus the ones already on, so a granular consent screen
    # confirms them together.
    assert q["scope"].split() == ["openid", "email", P.DOCS_SCOPE, P.DRIVE_FILE_SCOPE]
    assert connection.enabled_products("alice") == ["drive", "docs"]
    st = connection.status("alice")
    assert P.DRIVE_FILE_SCOPE in st["scopes"] and P.DOCS_SCOPE in st["scopes"]
    assert REFRESH not in json.dumps(st) and REFRESH2 not in json.dumps(st)
    files = list((tmp_path / "data" / "connections").iterdir())
    assert len(files) == 1
    text = files[0].read_text()
    assert REFRESH2 not in text and REFRESH not in text and "enc:" in text       # encrypted at rest
    if os.name == "posix":
        assert (files[0].stat().st_mode & 0o777) == 0o600
    assert store.decrypt(json.loads(text)["refresh_token"]) == REFRESH2


def test_a_product_google_did_not_grant_is_reported(google):
    ok, msg, _ = _connect(google, ["sheets"], grant=[])
    assert not ok and "Google Sheets" in msg
    ok, msg, _ = _connect(google, ["drive", "drive_all"], grant=[P.DRIVE_FILE_SCOPE])
    assert ok and "Not granted: Read all my Drive files" in msg
    assert connection.enabled_products("alice") == ["drive"]


# ── What the grant really holds (tokeninfo) ──────────────────────────────

LOST_DOCS = ("Google did not keep access to Docs. Connect Docs again and keep every box ticked on "
             "Google's consent screen.")


def _tokeninfo_calls(fake):
    return [c for c in fake.calls if c["url"].startswith(connection.TOKENINFO_URL)]


def _drive_then_docs(fake):
    assert _connect(fake, ["drive"])[0]
    assert _connect(fake, ["docs"])[0]
    assert connection.enabled_products("alice") == ["drive", "docs"]


def test_tokeninfo_keeps_a_scope_the_token_response_left_out(google):
    """The live bug: Drive, then Docs, then Sheets; the Sheets token
    response's `scope` leaves out documents though the grant still has it."""
    _drive_then_docs(google)
    google.returned = [P.DRIVE_FILE_SCOPE, P.SHEETS_SCOPE]
    ok, msg, q = _connect(google, ["sheets"])
    # Every product already on is asked for again alongside the new one.
    assert set(q["scope"].split()) == {"openid", "email", P.SHEETS_SCOPE, P.DRIVE_FILE_SCOPE, P.DOCS_SCOPE}
    assert store._read_json(connection._states_file()) == {}
    assert ok and "Google Sheets" in msg and "did not keep" not in msg
    assert connection.enabled_products("alice") == ["drive", "docs", "sheets"]
    st = connection.status("alice")
    assert P.DOCS_SCOPE in st["scopes"] and st["checked_at"] > 0
    assert not any(p["needs_reconnect"] for p in st["products"])
    assert _tokeninfo_calls(google)


def test_a_scope_google_really_dropped_shows_needs_reconnect(google):
    _drive_then_docs(google)
    # Unticked on the granular consent screen: neither the token response
    # nor tokeninfo has documents.
    ok, msg, _ = _connect(google, ["sheets"], grant=[P.DRIVE_FILE_SCOPE, P.SHEETS_SCOPE])
    assert ok and "Connected Google Sheets" in msg and LOST_DOCS in msg
    assert connection.enabled_products("alice") == ["drive", "sheets"]
    assert connection.needs_reconnect("alice") == ["docs"]
    docs_card = {p["key"]: p for p in connection.status("alice")["products"]}["docs"]
    assert docs_card["needs_reconnect"] and not docs_card["enabled"]
    assert "Needs reconnect" in tools.status_text("alice") and "Google Docs" in tools.status_text("alice")
    # Reconnect asks for Docs with everything else that is on, and fixes it.
    ok, msg, q = _connect(google, ["docs"])
    assert P.SHEETS_SCOPE in q["scope"].split() and P.DOCS_SCOPE in q["scope"].split()
    assert ok and "did not keep" not in msg
    assert connection.enabled_products("alice") == ["drive", "docs", "sheets"]
    assert connection.needs_reconnect("alice") == []
    # A connect that grants nothing new still says what was lost.
    ok, msg, _ = _connect(google, ["slides"], grant=[P.DRIVE_FILE_SCOPE, P.SHEETS_SCOPE])
    assert not ok and "Google Slides" in msg and LOST_DOCS in msg


def test_tokeninfo_failing_does_not_carry_scopes_to_another_account(google):
    _drive_then_docs(google)
    google.tokeninfo = "error"
    google.email = "someone-else@example.com"
    google.refresh_token = REFRESH2
    ok, msg, _ = _connect(google, ["sheets"], grant=[P.SHEETS_SCOPE])
    assert ok and "someone-else@example.com" in msg and "did not keep" not in msg
    d = connection.load("alice")
    assert P.DOCS_SCOPE not in d["scopes"].split() and P.DRIVE_FILE_SCOPE not in d["scopes"].split()
    assert connection.enabled_products("alice") == ["sheets"] and connection.needs_reconnect("alice") == []


def test_tokeninfo_for_another_client_is_not_trusted(google):
    _drive_then_docs(google)
    google.aud = "999-other.apps.googleusercontent.com"
    google.returned = [P.DRIVE_FILE_SCOPE, P.SHEETS_SCOPE]
    ok, _, _ = _connect(google, ["sheets"])
    # Not trusted: what the same account had plus what came back.
    assert ok and connection.enabled_products("alice") == ["drive", "docs", "sheets"]


# ── Tokens ───────────────────────────────────────────────────────────────

def test_access_tokens_refresh_and_a_401_is_retried_once(google):
    _connect(google, ["tasks"])
    connection._access.clear()
    out = run("google_tasks_list", {})
    assert out["exit_code"] == 0 and google.refreshes == 1
    run("google_tasks_list", {})
    assert google.refreshes == 1                                        # cached
    google.expire_once = True
    out = run("google_tasks_list", {})
    assert out["exit_code"] == 0 and google.refreshes == 2              # fresh token after the 401


def test_a_revoked_grant_is_forgotten_with_a_way_back(google):
    _connect(google, ["contacts"])
    connection._access.clear()
    google.refresh = "invalid_grant"
    out = run("google_contacts_search", {"query": "amy"})
    assert out["exit_code"] == 1 and "Google add-on" in out["error"]
    assert connection.status("alice")["connected"] is False


# ── Owner isolation, hiding, disconnecting ───────────────────────────────

def test_each_user_has_their_own_connection(google):
    _connect(google, ["docs"], user="alice")
    assert connection.enabled_products("bob") == []
    out = run("google_doc_read", {"document": DOC_ID}, user="bob")
    assert out["exit_code"] == 1 and "not connected" in out["error"]
    assert not any(c["url"].startswith("https://docs.googleapis.com") for c in google.calls)
    assert run("google_doc_read", {"document": DOC_ID}, user="alice")["exit_code"] == 0


# ── Drive ────────────────────────────────────────────────────────────────

def test_drive_search_request_and_output(google):
    _connect(google, ["drive"])
    out = run("google_drive_search", {"query": "taxes 'x'", "kind": "doc", "limit": 500})
    c = google.last("GET", "/drive/v3/files")
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(c["url"]).query))
    assert "trashed = false" in q["q"] and "name contains 'taxes \\'x\\''" in q["q"]
    assert P_GDOC in q["q"] and q["pageSize"] == "50" and "orderBy" not in q      # no sort on full-text
    assert "Taxes 2026 | Google Doc | 2026-10-01 10:00 | id " + DOC_ID in out["output"]
    assert "page_token=NEXT" in out["output"]
    run("google_drive_search", {})
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(google.last("GET", "/drive/v3/files")["url"]).query))
    assert q["orderBy"] == "modifiedTime desc"


def test_drive_read_exports_docs_as_markdown_and_cuts_long_content(google):
    _connect(google, ["drive"])
    google.doc_text = "# Taxes\n" + "a" * 30000
    out = run("google_drive_read", {"file": f"https://docs.google.com/document/d/{DOC_ID}/edit", "max_chars": 1000})
    c = google.last("GET", "/export")
    assert "mimeType=text%2Fmarkdown" in c["url"]
    assert "Name: Taxes 2026" in out["output"] and "# Taxes" in out["output"]
    assert "offset=1000" in out["output"] and len(out["output"]) < 2000
    out = run("google_drive_read", {"file": DOC_ID, "offset": 29500})
    assert "the end" in out["output"]


def test_drive_read_outside_narrow_access_explains(google):
    _connect(google, ["drive"])
    out = run("google_drive_read", {"file": FILE_ID})
    assert out["exit_code"] == 1 and "Read all my Drive files" in out["error"]


def test_drive_upload_is_multipart_and_converts(google):
    _connect(google, ["drive"])
    out = run("google_drive_upload", {"name": "notes.md", "content": "# Hi", "convert": "doc",
                                      "folder_id": "https://drive.google.com/drive/folders/1FolderFolderFolder"})
    c = google.last("POST", "/upload/drive/v3/files")
    assert "uploadType=multipart" in c["url"] and c["ctype"].startswith("multipart/related; boundary=")
    meta = json.loads(c["body"].split("\r\n\r\n", 1)[1].split("\r\n", 1)[0])
    assert meta == {"name": "notes", "parents": ["1FolderFolderFolder"], "mimeType": P_GDOC}
    assert "# Hi" in c["body"]
    assert out["google_link"]["url"].startswith("https://docs.google.com/document/d/")


def test_drive_create_folder(google):
    _connect(google, ["drive"])
    out = run("google_drive_create_folder", {"name": "Receipts"})
    body = json.loads(google.last("POST", "/drive/v3/files")["body"])
    assert body == {"name": "Receipts", "mimeType": "application/vnd.google-apps.folder"}
    assert out["google_link"]["url"] == "https://drive.google.com/drive/folders/1FolderFolderFolder"


# ── Docs ─────────────────────────────────────────────────────────────────

def test_doc_read_is_markdownish(google):
    _connect(google, ["docs"])
    out = run("google_doc_read", {"document": DOC_ID})["output"]
    assert "# Goals\n- Ship\n1. Test\n| Name | Due |\n| --- | --- |\n| Docs | Fri |\nThe end." in out


def test_doc_create_formats_headings_bullets_and_bold(google):
    _connect(google, ["docs"])
    out = run("google_doc_create", {"title": "Plan", "content": "# Goals\n- **Ship** it 🚀\n- Test\nDone"})
    made = [c for c in google.calls if c["url"].endswith("/v1/documents")]
    assert json.loads(made[-1]["body"]) == {"title": "Plan"}
    reqs = json.loads(google.last("POST", ":batchUpdate")["body"])["requests"]
    assert reqs[0] == {"insertText": {"location": {"index": 1}, "text": "Goals\nShip it 🚀\nTest\nDone\n"}}
    styles = [r["updateParagraphStyle"] for r in reqs if "updateParagraphStyle" in r]
    assert styles[0]["range"] == {"startIndex": 1, "endIndex": 7}
    assert styles[0]["paragraphStyle"]["namedStyleType"] == "HEADING_1"
    bullets = [r["createParagraphBullets"] for r in reqs if "createParagraphBullets" in r]
    # "Ship it 🚀" is 10 UTF-16 units (the rocket is two), plus its newline.
    assert bullets == [{"range": {"startIndex": 7, "endIndex": 23}, "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE"}]
    bold = [r["updateTextStyle"] for r in reqs if "updateTextStyle" in r and r["updateTextStyle"]["textStyle"]["bold"]]
    assert bold[0]["range"] == {"startIndex": 7, "endIndex": 11}
    assert out["google_link"] == {"name": "Plan", "url": f"https://docs.google.com/document/d/{DOC_ID}/edit",
                                  "verb": "New Google Doc"}


def test_doc_edit_replace_and_append(google):
    _connect(google, ["docs"])
    out = run("google_doc_edit", {"document": DOC_ID, "action": "replace", "find": "Fri", "replace": "Mon"})
    req = json.loads(google.last("POST", ":batchUpdate")["body"])["requests"][0]
    assert req == {"replaceAllText": {"containsText": {"text": "Fri", "matchCase": True}, "replaceText": "Mon"}}
    assert "Replaced 2 occurrences" in out["output"]
    run("google_doc_edit", {"document": DOC_ID, "action": "append", "text": "## Next"})
    reqs = json.loads(google.last("POST", ":batchUpdate")["body"])["requests"]
    assert reqs[0] == {"insertText": {"location": {"index": 49}, "text": "\nNext"}}
    assert reqs[1]["updateParagraphStyle"]["range"] == {"startIndex": 50, "endIndex": 55}
    assert run("google_doc_edit", {"document": DOC_ID, "action": "nope"})["exit_code"] == 1


def test_ids_never_reach_a_url_unchecked(google):
    _connect(google, ["docs"])
    for bad in ("../../drive/v3/files", "abc/def?x=1", "short"):
        out = run("google_doc_read", {"document": bad})
        assert out["exit_code"] == 1, bad
    assert common.file_id(f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid=0") == SHEET_ID
    assert common.file_id(f"https://drive.google.com/open?id={FILE_ID}") == FILE_ID


# ── Sheets ───────────────────────────────────────────────────────────────

def test_sheet_read_lists_tabs_and_trims_with_a_next_range(google):
    _connect(google, ["sheets"])
    out = run("google_sheet_read", {"spreadsheet": SHEET_ID, "range": "Sheet1!A1:C250", "max_rows": 100})["output"]
    assert "Tabs: Sheet1 (1000 rows x 26 cols); Notes (10 rows x 2 cols)" in out
    assert "| r99 | 99 |" in out and "| r100 |" not in out
    assert "range='Sheet1!A101:ZZ'" in out
    assert "x" * 301 not in out                                          # long cells cut
    c = google.last("GET", "/values/")
    assert "/values/Sheet1%21A1%3AC250" in c["url"] and "valueRenderOption=FORMATTED_VALUE" in c["url"]


def test_sheet_write_append_create_use_user_entered(google):
    _connect(google, ["sheets"])
    run("google_sheet_write", {"spreadsheet": SHEET_ID, "range": "Sheet1!A1", "rows": [["a", "=SUM(B1:B9)"]]})
    c = google.last("PUT", "/values/")
    assert "valueInputOption=USER_ENTERED" in c["url"]
    assert json.loads(c["body"])["values"] == [["a", "=SUM(B1:B9)"]]
    out = run("google_sheet_append", {"spreadsheet": SHEET_ID, "rows": [["x", 1]]})
    c = google.last("POST", ":append")
    assert "/values/Sheet1:append" in c["url"] and "insertDataOption=INSERT_ROWS" in c["url"]
    assert "Added 1 rows" in out["output"]
    out = run("google_sheet_create", {"title": "Budget", "tabs": ["Budget"], "rows": [["Item", "Cost"]]})
    made = [c for c in google.calls if c["url"].endswith("/v4/spreadsheets") and c["method"] == "POST"]
    assert json.loads(made[-1]["body"])["sheets"] == [{"properties": {"title": "Budget"}}]
    assert "%27Budget%27%21A1" in google.last("PUT", "/values/")["url"]
    assert out["google_link"]["url"].endswith(f"{SHEET_ID}/edit")
    assert run("google_sheet_write", {"spreadsheet": SHEET_ID, "range": "A1", "rows": "nope"})["exit_code"] == 1


# ── Slides, Tasks, Contacts ──────────────────────────────────────────────

def test_slides_read_and_create(google):
    _connect(google, ["slides"])
    out = run("google_slides_read", {"presentation": DECK_ID})["output"]
    assert "## Slide 1\nWhy now" in out and "Notes: Say it slowly" in out
    out = run("google_slides_create", {"title": "Pitch", "subtitle": "Q4",
                                       "slides": [{"title": "Problem", "bullets": ["Slow", "Costly"]}]})
    reqs = json.loads(google.last("POST", ":batchUpdate")["body"])["requests"]
    layouts = [r["createSlide"]["slideLayoutReference"]["predefinedLayout"] for r in reqs if "createSlide" in r]
    assert layouts == ["TITLE", "TITLE_AND_BODY"]
    texts = [r["insertText"]["text"] for r in reqs if "insertText" in r]
    assert texts == ["Pitch", "Q4", "Problem", "Slow\nCostly"]
    assert reqs[-1] == {"deleteObject": {"objectId": "p"}}
    assert "2 slides" in out["output"]


def test_tasks_list_and_create(google):
    _connect(google, ["tasks"])
    out = run("google_tasks_list", {})["output"]
    assert "[ ] Pay rent | due 2026-10-20 | id t1" in out
    assert "lists/%40default/tasks" in google.last("GET", "/tasks")["url"] or "lists/@default/tasks" in google.last("GET", "/tasks")["url"]
    assert "My Tasks | id MDE" in run("google_tasks_list", {"tasklist": "lists"})["output"]
    out = run("google_task_create", {"title": "Call Amy", "due": "2026-10-21"})
    assert json.loads(google.last("POST", "/tasks")["body"]) == {"title": "Call Amy", "due": "2026-10-21T00:00:00.000Z"}
    assert run("google_tasks_list", {"tasklist": "../x"})["exit_code"] == 1


def test_contacts_search(google):
    _connect(google, ["contacts"])
    out = run("google_contacts_search", {"query": "amy"})["output"]
    assert out == "Amy Pond | amy@example.com | +1 555 0100 | CTO TARDIS"
    c = google.last("GET", "people:searchContacts")
    assert "readMask=names%2CemailAddresses%2CphoneNumbers%2Corganizations" in c["url"]


def test_a_disabled_api_says_how_to_enable_it(google):
    _connect(google, ["sheets"])
    google.api_status = 403
    out = run("google_sheet_read", {"spreadsheet": SHEET_ID})
    assert out["exit_code"] == 1 and "Google Sheets API is not enabled" in out["error"]


# ── Links in chat ────────────────────────────────────────────────────────

def test_only_google_links_go_into_the_reply():
    assert common.link_line({"name": "Plan", "url": f"https://docs.google.com/document/d/{DOC_ID}/edit",
                             "verb": "New Google Doc"}).strip() == \
        f"[New Google Doc: Plan](https://docs.google.com/document/d/{DOC_ID}/edit)"
    assert common.link_line({"name": "x", "url": "https://evil.example/docs.google.com"}) == ""
    assert common.link_line({"name": "x", "url": "javascript:alert(1)"}) == ""


