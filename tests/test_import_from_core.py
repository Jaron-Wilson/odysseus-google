"""Carrying Odysseus's Google connections over (odysseus_google/import_from_core.py),
against a fake Odysseus data folder: a copy (the core's files are untouched),
secrets re-encrypted with the add-on's key, Calendar-only connections
skipped, nothing secret printed."""
import io
import json
import os

import pytest
from cryptography.fernet import Fernet

from odysseus_google import connection, import_from_core, store

REFRESH = "1//core-refresh-token-never-shown"
SECRET = "core-client-secret-never-shown"
CID = "123-abc.apps.googleusercontent.com"


def _core(tmp_path, client_in="settings"):
    data = tmp_path / "odysseus-data"
    (data / "google_calendar").mkdir(parents=True)
    key = Fernet.generate_key()
    (data / ".app_key").write_bytes(key)
    f = Fernet(key)
    enc = lambda s: "enc:" + f.encrypt(s.encode()).decode()  # noqa: E731
    files = {
        # alice: Calendar plus Docs and Drive (key = the core's _key("alice"))
        connection._key("alice"): {"email": "alice@example.com", "refresh_token": enc(REFRESH),
                                   "scopes": "openid email https://www.googleapis.com/auth/documents "
                                             "https://www.googleapis.com/auth/drive.file",
                                   "products": ["calendar", "docs", "drive"], "connected_at": 1},
        # bob: Calendar only
        connection._key("bob"): {"email": "bob@example.com", "refresh_token": enc("bob-rt"),
                                 "scopes": "openid email", "products": ["calendar"]},
    }
    for k, v in files.items():
        (data / "google_calendar" / f"{k}.json").write_text(json.dumps(v))
    env = tmp_path / ".env"
    if client_in == "settings":
        (data / "settings.json").write_text(json.dumps({"google_oauth_client_id": CID,
                                                       "google_oauth_client_secret": SECRET}))
        env.write_text("OTHER=1\n")
    else:
        env.write_text(f"GOOGLE_OAUTH_CLIENT_ID={CID}\nexport GOOGLE_OAUTH_CLIENT_SECRET='{SECRET}'\nOPENAI_API_KEY=x\n")
    return data, env


def _snapshot(path):
    return {p: open(os.path.join(dp, p), "rb").read() for dp, _, fs in os.walk(path) for p in fs}


@pytest.mark.parametrize("client_in", ["settings", "env"])
def test_import_copies_connections_and_the_client(tmp_path, monkeypatch, client_in):
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    data, env = _core(tmp_path, client_in)
    before = _snapshot(data)
    mod_data = data / "mods" / "google" / "data"
    out = io.StringIO()
    rep = import_from_core.run(str(data), str(mod_data), str(env), out=out)
    assert rep["imported"] == 1 and rep["skipped"] == 1 and rep["client"] == "copied"
    assert _snapshot(data / "google_calendar") == {k: v for k, v in before.items() if k.endswith(".json") and k != "settings.json"}
    # The add-on reads it with its own key.
    monkeypatch.setenv("MOD_DATA_DIR", str(mod_data))
    cfg = store.client_config()
    assert cfg["client_id"] == CID and cfg["client_secret"] == SECRET
    d = connection.load("alice")
    assert d["products"] == ["docs", "drive"] and store.decrypt(d["refresh_token"]) == REFRESH
    assert connection.enabled_products("alice") == ["docs", "drive"]
    assert connection.load("bob") == {}
    # Encrypted at rest, 0600, and nothing secret in what it printed.
    for p in (mod_data / "client.json", mod_data / "connections" / f"{connection._key('alice')}.json"):
        text = p.read_text()
        assert REFRESH not in text and SECRET not in text
        assert (p.stat().st_mode & 0o777) == 0o600
    printed = out.getvalue()
    assert REFRESH not in printed and SECRET not in printed and "alice@example.com" not in printed
    assert "imported: 1" in printed


def test_dry_run_writes_nothing_and_existing_ones_are_kept(tmp_path, monkeypatch):
    data, env = _core(tmp_path)
    mod_data = data / "mods" / "google" / "data"
    rep = import_from_core.run(str(data), str(mod_data), str(env), dry_run=True, out=io.StringIO())
    assert rep["imported"] == 1 and not (mod_data / "connections").exists() and not (mod_data / "client.json").exists()
    import_from_core.run(str(data), str(mod_data), str(env), out=io.StringIO())
    rep = import_from_core.run(str(data), str(mod_data), str(env), out=io.StringIO())
    assert rep["imported"] == 0 and rep["kept"] == 1 and rep["client"] == "kept the add-on's own"
    rep = import_from_core.run(str(data), str(mod_data), str(env), overwrite=True, out=io.StringIO())
    assert rep["imported"] == 1 and rep["client"] == "copied"


def test_without_sign_in_the_local_user_is_the_cores_default(tmp_path, monkeypatch):
    """With sign-in off the core's owner is "" and the add-on is called as "local"."""
    assert connection._key("local") == connection._key("") == connection._key(None)
    assert connection._key("alice") != connection._key("")
