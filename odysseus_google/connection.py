"""The Google connection each user has in this add-on: which products are
on, the refresh token, access tokens, incremental consent and revoke.

It works like Odysseus's own Google connection (src/google_workspace/
connection.py in the core, which keeps Google Calendar and Meet): one per
user, `access_type=offline` so there is a refresh token, and each Connect
asks for the new product's scopes plus those of every product already on,
with `include_granted_scopes=true`. Google's tokeninfo endpoint, not the
token response, says which scopes the grant really holds.

Storage, all under MOD_DATA_DIR (0600 files):

    client.json            the Google OAuth client (id, secret encrypted, redirect URI override)
    connections/<key>.json  one per user: email, scopes, products, encrypted refresh token
    oauth_states.json      Connects in progress (ten minutes)
    secret.key             the Fernet key that encrypts the secrets above

The refresh token and the client secret never leave the server: no page or
tool answer carries them, and they are never logged.

Tests point every Google call at a fake with GOOGLE_ADDON_API_BASE (an
environment variable only, never a setting) or swap in `_transport`.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import secrets
import time
import urllib.parse
from typing import Dict, List, Optional, Tuple

import httpx

from odysseus_google import products as P

logger = logging.getLogger(__name__)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"
CALLBACK = "oauth/callback"        # under the add-on: /mods/google/oauth/callback
STATE_TTL_S = 600
RECHECK_S = 300
API_BASE_ENV = "GOOGLE_ADDON_API_BASE"
RECONNECT = "Connect it again in the Google add-on's settings."

# Tests swap in an httpx.MockTransport; nothing here talks to Google then.
_transport: Optional[httpx.AsyncBaseTransport] = None
# user key -> (access token, expires at)
_access: Dict[str, Tuple[str, float]] = {}


class GoogleError(Exception):
    """A sentence for the user about why it did not work."""


class _RedactAccessToken(logging.Filter):
    """httpx logs request URLs, and tokeninfo takes the token in the query."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and any("access_token=" in str(a) for a in record.args):
            record.args = tuple(re.sub(r"access_token=[^&\s\"]*", "access_token=[redacted]", str(a))
                                if "access_token=" in str(a) else a for a in record.args)
        return True


logging.getLogger("httpx").addFilter(_RedactAccessToken())


def api_url(url: str) -> str:
    base = os.environ.get(API_BASE_ENV, "").strip().rstrip("/")
    if not base:
        return url
    p = urllib.parse.urlsplit(url)
    return f"{base}/{p.netloc}{p.path}" + (f"?{p.query}" if p.query else "")


# ── files, secrets and the OAuth client: odysseus_google/store.py ──────────

from odysseus_google.store import (  # noqa: E402,F401
    _lock, _read_json, _write_json, client_config, data_dir, decrypt, encrypt, may_edit_client, save_client,
)


def redirect_uri(headers, base_path: str = "") -> str:
    """Google compares this string exactly, so it is fixed: the add-on's
    path without the frame token, on the address Odysseus is opened at."""
    override = client_config()["redirect_uri"]
    if override:
        return override
    host = headers.get("X-Forwarded-Host") or headers.get("Host") or "127.0.0.1"
    proto = headers.get("X-Forwarded-Proto") or "http"
    prefix = (base_path or "").split("/~")[0].rstrip("/")
    return f"{proto}://{host}{prefix}/{CALLBACK}"


# ── per-user connections ─────────────────────────────────────────────────

def _key(user: Optional[str]) -> str:
    """The same file name the core uses for this user (so an import keeps
    them). Without sign-in the core calls the user "" and the add-on gets
    "local": both are the same person."""
    u = user or ""
    if u in ("local", "odysseus-core"):
        u = ""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", u)[:40] or "_default"
    return f"{safe}-{hashlib.sha256(u.encode('utf-8')).hexdigest()[:8]}"


def _path(user: Optional[str]) -> str:
    return os.path.join(data_dir(), "connections", _key(user) + ".json")


def load(user: Optional[str]) -> Dict:
    return _read_json(_path(user))


def _save(user: Optional[str], data: Dict) -> None:
    _write_json(_path(user), data)


def _forget(user: Optional[str]) -> None:
    _access.pop(_key(user), None)
    try:
        os.remove(_path(user))
    except FileNotFoundError:
        pass


def _refresh_token(user: Optional[str]) -> str:
    return decrypt(str(load(user).get("refresh_token") or ""))


def _stored_products(d: Dict) -> List[str]:
    return [str(p) for p in d.get("products") or [] if str(p) in P.PRODUCTS]


def _granted(product: str, scopes: List[str]) -> bool:
    need = (P.PRODUCTS.get(product) or {}).get("scopes") or []
    return bool(need) and all(s in scopes for s in need)


def enabled_products(user: Optional[str]) -> List[str]:
    d = load(user)
    if not d.get("refresh_token"):
        return []
    scopes = str(d.get("scopes") or "").split()
    on = [p for p in _stored_products(d) if _granted(p, scopes)]
    return [p for p in on if not P.PRODUCTS[p].get("parent") or P.PRODUCTS[p]["parent"] in on]


def is_enabled(user: Optional[str], product: str) -> bool:
    return product in enabled_products(user)


def needs_reconnect(user: Optional[str]) -> List[str]:
    d = load(user)
    if not d.get("refresh_token"):
        return []
    scopes = str(d.get("scopes") or "").split()
    return [p for p in _stored_products(d) if not _granted(p, scopes)]


def _short(product: str) -> str:
    label = P.PRODUCTS[product]["label"]
    return label[len("Google "):] if label.startswith("Google ") else label


def lost_message(products: List[str]) -> str:
    return " ".join(f"Google did not keep access to {_short(p)}. Connect {_short(p)} again and keep "
                    "every box ticked on Google's consent screen." for p in products)


def status(user: Optional[str]) -> Dict:
    """What the settings page shows. Nothing secret."""
    d = load(user)
    scopes = str(d.get("scopes") or "").split()
    on = enabled_products(user)
    lost = needs_reconnect(user)
    items = []
    for key, p in P.PRODUCTS.items():
        items.append({"key": key, "label": p["label"], "about": p["about"], "api": p["api"],
                      "parent": p.get("parent") or "", "scopes": list(p["scopes"]),
                      "granted": _granted(key, scopes), "enabled": key in on, "needs_reconnect": key in lost,
                      "tools": P.tools_for(key)})
    return {"configured": client_config()["configured"], "connected": bool(d.get("refresh_token")),
            "email": str(d.get("email") or ""), "scopes": scopes, "connected_at": d.get("connected_at") or 0,
            "checked_at": d.get("checked_at") or 0,
            "imported": bool(d.get("imported_from_core")), "products": items}


def clean_products(raw) -> List[str]:
    items = raw if isinstance(raw, list) else re.split(r"[,\s]+", str(raw or ""))
    out: List[str] = []
    for k in items:
        k = str(k or "").strip().lower()
        if k in P.PRODUCTS and k not in out:
            out.append(k)
    for k in list(out):
        parent = P.PRODUCTS[k].get("parent")
        if parent and parent not in out:
            out.insert(0, parent)
    return out


# ── OAuth states ─────────────────────────────────────────────────────────

def _states_file() -> str:
    return os.path.join(data_dir(), "oauth_states.json")


def remember_state(payload: Dict) -> str:
    state = secrets.token_urlsafe(24)
    with _lock:
        d = _read_json(_states_file())
        now = time.time()
        d = {k: v for k, v in d.items() if isinstance(v, dict) and now - float(v.get("at") or 0) < STATE_TTL_S}
        d[state] = dict(payload, at=now)
        _write_json(_states_file(), d)
    return state


def pop_state(state: str) -> Optional[Dict]:
    with _lock:
        d = _read_json(_states_file())
        entry = d.pop(state or "", None)
        _write_json(_states_file(), d)
    if not isinstance(entry, dict) or time.time() - float(entry.get("at") or 0) > STATE_TTL_S:
        return None
    return entry


def _pkce() -> Tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:128]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode()
    return verifier, challenge


def connect_url(user: Optional[str], products: List[str], uri: str) -> str:
    """Google's consent page for these products plus the ones already on."""
    cfg = client_config()
    if not cfg["configured"]:
        raise GoogleError("No Google OAuth client is set up in the Google add-on yet.")
    products = clean_products(products)
    if not products:
        raise GoogleError("Pick a Google product to connect.")
    d = load(user)
    on = _stored_products(d) if d.get("refresh_token") else []
    verifier, challenge = _pkce()
    state = remember_state({"user": user or "", "products": products, "redirect_uri": uri,
                            "code_verifier": verifier})
    q = {"client_id": cfg["client_id"], "redirect_uri": uri, "response_type": "code", "state": state,
         "code_challenge": challenge, "code_challenge_method": "S256",
         "scope": " ".join(P.scopes_for(products + [p for p in on if p not in products])),
         "access_type": "offline", "prompt": "consent", "include_granted_scopes": "true"}
    return api_url(AUTH_URL) + "?" + urllib.parse.urlencode(q)


def _claims(id_token: str) -> Dict:
    try:
        payload = id_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
    except Exception:
        return {}


def client(timeout: float = 30) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout, transport=_transport, trust_env=False)


async def _revoke(token: str) -> None:
    if not token:
        return
    try:
        async with client(10) as c:
            await c.post(api_url(REVOKE_URL), data={"token": token})
    except httpx.HTTPError:
        pass


async def _tokeninfo(access: str) -> Optional[List[str]]:
    if not access:
        return None
    try:
        async with client(10) as c:
            r = await c.get(api_url(TOKENINFO_URL), params={"access_token": access})
        if r.status_code != 200:
            return None
        info = r.json()
    except Exception as e:
        logger.info("[google] tokeninfo: %s", type(e).__name__)
        return None
    if not isinstance(info, dict) or "scope" not in info:
        return None
    aud = str(info.get("aud") or info.get("azp") or "")
    cid = client_config()["client_id"]
    if aud and cid and aud != cid:
        logger.warning("[google] tokeninfo: the token belongs to another OAuth client")
        return None
    return str(info.get("scope") or "").split()


async def recheck(user: Optional[str], force: bool = False) -> Dict:
    """Ask Google which scopes the grant holds now and save them. Never raises."""
    nothing = {"checked": False, "lost": []}
    d = load(user)
    if not d.get("refresh_token"):
        return nothing
    if not force and time.time() - float(d.get("checked_at") or 0) < RECHECK_S:
        return nothing
    try:
        async with client(15) as c:
            access = await token(c, user)
    except GoogleError:
        return nothing
    scopes = await _tokeninfo(access)
    if scopes is None:
        return nothing
    now = load(user)
    if now.get("refresh_token") != d.get("refresh_token"):
        return nothing
    before = str(now.get("scopes") or "").split()
    lost = [p for p in _stored_products(now) if _granted(p, before) and not _granted(p, scopes)]
    now.update(scopes=" ".join(scopes), checked_at=time.time())
    _save(user, now)
    return {"checked": True, "lost": lost}


async def finish_connect(user: Optional[str], code: str, entry: Dict) -> Tuple[bool, str]:
    """Trade the code for tokens and keep the refresh token. (ok, message)."""
    asked = clean_products(entry.get("products") or [])
    cfg = client_config()
    if not cfg["configured"]:
        return False, "No Google OAuth client is set up in the Google add-on."
    data = {"code": code, "client_id": cfg["client_id"], "client_secret": cfg["client_secret"],
            "redirect_uri": entry.get("redirect_uri", ""), "grant_type": "authorization_code",
            "code_verifier": entry.get("code_verifier", "")}
    try:
        async with client(20) as c:
            r = await c.post(api_url(TOKEN_URL), data=data)
    except httpx.HTTPError as e:
        logger.warning("[google] connect: %s", type(e).__name__)
        return False, "Could not reach Google to finish connecting."
    if r.status_code != 200:
        logger.warning("[google] connect: token endpoint said %s", r.status_code)
        return False, ("Google turned the connection down. The redirect URI must match one registered "
                       f"in the Google console exactly: {entry.get('redirect_uri', '')}")
    tok = r.json()
    returned = str(tok.get("scope") or "").split()
    email = str(_claims(str(tok.get("id_token") or "")).get("email") or "")
    old = load(user)
    old_refresh = _refresh_token(user)
    same = not old.get("email") or not email or old.get("email") == email
    refresh = str(tok.get("refresh_token") or "") or (old_refresh if same else "")
    if not refresh:
        return False, ("Google did not send a refresh token. Remove this app from your Google account's "
                       "third-party access, then connect again.")
    old_on = _stored_products(old) if same and old.get("refresh_token") else []
    old_scopes = str(old.get("scopes") or "").split() if old_on else []
    verified = await _tokeninfo(str(tok.get("access_token") or ""))
    granted = verified if verified is not None else list(dict.fromkeys(old_scopes + returned))
    got = [p for p in asked if _granted(p, granted)]
    missing = [p for p in asked if p not in got]
    lost = [p for p in old_on if p not in asked and _granted(p, old_scopes) and not _granted(p, granted)]
    if not got:
        names = ", ".join(P.PRODUCTS[p]["label"] for p in missing)
        msg = f"Google did not grant access to {names}. Connect again and leave its box ticked."
        return False, (msg + " " + lost_message(lost)) if lost else msg
    products = list(dict.fromkeys(old_on + got))
    _save(user, {"email": email or str(old.get("email") or ""), "refresh_token": encrypt(refresh),
                 "scopes": " ".join(granted), "products": products,
                 "checked_at": time.time() if verified is not None else 0, "connected_at": time.time()})
    _access.pop(_key(user), None)
    if tok.get("access_token"):
        _access[_key(user)] = (str(tok["access_token"]), time.time() + int(tok.get("expires_in") or 3600) - 60)
    logger.info("[google] connected %s", ",".join(got))
    extra = ""
    if missing:
        extra += " Not granted: " + ", ".join(P.PRODUCTS[p]["label"] for p in missing) + "."
    if lost:
        extra += " " + lost_message(lost)
    return True, (f"Connected {', '.join(P.PRODUCTS[p]['label'] for p in got)} as "
                  f"{email or 'your Google account'}.{extra} You can close this tab.")


async def disconnect(user: Optional[str], product: Optional[str] = None, revoke: bool = False) -> None:
    """Stop using one product, or forget the connection. Google is asked to
    revoke the grant only when `revoke` is set: with the same OAuth client,
    the grant is shared with Odysseus's own Google Calendar connection."""
    d = load(user)
    if product and d.get("refresh_token"):
        left = [p for p in _stored_products(d) if p != product and P.PRODUCTS[p].get("parent") != product]
        if left:
            d["products"] = left
            _save(user, d)
            return
    refresh = _refresh_token(user)
    _forget(user)
    if revoke:
        await _revoke(refresh)


# ── calling Google ───────────────────────────────────────────────────────

async def token(c: httpx.AsyncClient, user: Optional[str], fresh: bool = False) -> str:
    k = _key(user)
    cached = _access.get(k)
    if cached and not fresh and cached[1] > time.time():
        return cached[0]
    refresh = _refresh_token(user)
    if not refresh:
        raise GoogleError("Google is not connected. " + RECONNECT)
    cfg = client_config()
    if not cfg["configured"]:
        raise GoogleError("The Google OAuth client is not set up in the Google add-on.")
    try:
        r = await c.post(api_url(TOKEN_URL), data={"client_id": cfg["client_id"], "client_secret": cfg["client_secret"],
                                                   "refresh_token": refresh, "grant_type": "refresh_token"})
    except httpx.HTTPError:
        raise GoogleError("Could not reach Google.")
    if r.status_code != 200:
        err = ""
        try:
            err = str(r.json().get("error") or "")
        except ValueError:
            pass
        if err in ("invalid_grant", "unauthorized_client", "invalid_client"):
            _forget(user)
            raise GoogleError("Google no longer accepts the saved connection (revoked or expired). " + RECONNECT)
        raise GoogleError(f"Google would not refresh access ({r.status_code}).")
    tok = r.json()
    access = str(tok.get("access_token") or "")
    _access[k] = (access, time.time() + int(tok.get("expires_in") or 3600) - 60)
    return access


async def call(c: httpx.AsyncClient, user: Optional[str], method: str, url: str, **kw) -> httpx.Response:
    """One API call with a fresh access token, retried once on a 401."""
    headers = dict(kw.pop("headers", None) or {})
    r = None
    for attempt in (0, 1):
        headers["Authorization"] = f"Bearer {await token(c, user, fresh=bool(attempt))}"
        try:
            r = await c.request(method, api_url(url), headers=headers, **kw)
        except httpx.HTTPError:
            raise GoogleError("Could not reach Google.")
        if r.status_code != 401:
            return r
    return r


def reason(r: httpx.Response) -> str:
    try:
        err = r.json().get("error") or {}
    except ValueError:
        return ""
    if not isinstance(err, dict):
        return str(err)
    reasons = " ".join(str(e.get("reason") or "") for e in err.get("errors") or [] if isinstance(e, dict))
    details = " ".join(str(d.get("reason") or "") for d in err.get("details") or [] if isinstance(d, dict))
    return f"{err.get('status') or ''} {reasons} {details} {err.get('message') or ''}"


def explain(r: httpx.Response, product: str, what: str = "") -> GoogleError:
    p = P.PRODUCTS.get(product) or {}
    why = reason(r)
    logger.info("[google] %s %s said %s: %s", product, what or "call", r.status_code, why.strip()[:200])
    if "SERVICE_DISABLED" in why or "accessNotConfigured" in why:
        return GoogleError(f"The {p.get('api', 'Google API')} is not enabled in the Google Cloud project "
                           "(APIs & Services > Library > Enable).")
    if r.status_code == 401 or "insufficient" in why.lower() or "ACCESS_TOKEN_SCOPE_INSUFFICIENT" in why:
        return GoogleError(f"Google did not accept the saved access to {p.get('label', 'Google')}. " + RECONNECT)
    if r.status_code == 404:
        hint = ""
        if product in ("drive", "drive_all", "docs", "sheets", "slides"):
            hint = (" Check the link. With Drive's narrow access the add-on only sees files it made or you "
                    "opened with it; 'Read all my Drive files' in its settings widens that.")
        return GoogleError(f"Google could not find that {what or 'item'}, or this account cannot open it.{hint}")
    if r.status_code == 403:
        return GoogleError(f"Google refused: this account is not allowed to {what or 'do that'} "
                           f"({why.strip()[:160] or 'forbidden'}).")
    if r.status_code == 429:
        return GoogleError("Google says too many requests right now; try again in a minute.")
    if r.status_code == 400:
        return GoogleError(f"Google did not accept the request: {why.strip()[:240] or 'bad request'}.")
    return GoogleError(f"Google {p.get('label', '')} answered {r.status_code}.".replace("  ", " "))
