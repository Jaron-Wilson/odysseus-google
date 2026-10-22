"""The add-on's files and secrets, standard library plus `cryptography` (no
httpx), so odysseus_google.import_from_core runs with any Python that has it.

    client.json   the Google OAuth client (secret encrypted)
    secret.key    the Fernet key for the secrets (0600)
"""
from __future__ import annotations

import json
import logging
import os
import threading
from typing import Dict, Optional

logger = logging.getLogger(__name__)
_lock = threading.Lock()


# ── files and secrets ────────────────────────────────────────────────────

def data_dir() -> str:
    return os.environ.get("MOD_DATA_DIR") or os.path.join(os.getcwd(), "data")


def _write_json(path: str, data: Dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def _read_json(path: str) -> Dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def _fernet():
    from cryptography.fernet import Fernet
    path = os.path.join(data_dir(), "secret.key")
    with _lock:
        if not os.path.exists(path):
            os.makedirs(data_dir(), exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(Fernet.generate_key())
        with open(path, "rb") as f:
            return Fernet(f.read().strip())


def encrypt(plain: str) -> str:
    if not plain:
        return ""
    return "enc:" + _fernet().encrypt(plain.encode()).decode()


def decrypt(value: str) -> str:
    if not value:
        return ""
    if not value.startswith("enc:"):
        return value
    try:
        return _fernet().decrypt(value[4:].encode()).decode()
    except Exception:
        logger.warning("[google] a stored secret could not be decrypted (wrong secret.key?)")
        return ""


# ── the OAuth client ─────────────────────────────────────────────────────

def _client_file() -> str:
    return os.path.join(data_dir(), "client.json")


def client_config() -> Dict:
    d = _read_json(_client_file())
    cid = str(d.get("client_id") or "").strip()
    sec = decrypt(str(d.get("client_secret") or ""))
    return {"client_id": cid, "client_secret": sec, "configured": bool(cid and sec),
            "redirect_uri": str(d.get("redirect_uri") or ""), "admins": list(d.get("admins") or [])}


def save_client(client_id: str, client_secret: str, redirect_uri: str = "", by: str = "") -> None:
    """An empty secret keeps the saved one. The first user to save it may
    change it later (the `admins` list); others only see it."""
    d = _read_json(_client_file())
    d["client_id"] = (client_id or "").strip()
    if (client_secret or "").strip():
        d["client_secret"] = encrypt(client_secret.strip())
    d["redirect_uri"] = (redirect_uri or "").strip()
    admins = list(d.get("admins") or [])
    if by and by not in admins:
        admins.append(by)
    d["admins"] = admins
    _write_json(_client_file(), d)


def may_edit_client(user: Optional[str]) -> bool:
    admins = client_config()["admins"]
    return not admins or (user or "") in admins


