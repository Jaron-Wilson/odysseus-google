"""Carry Odysseus's Google connections over to this add-on, so nobody has to
connect Drive, Docs, Sheets, Slides, Tasks or Contacts again.

    python3 -m odysseus_google.import_from_core --data-dir ~/odysseus-data --env-file ~/odysseus/.env

(run from the add-on's folder, <data dir>/mods/google/src; any Python with
the `cryptography` package works, the system one included). It reads, never
changes:

    <data dir>/.app_key                     the key Odysseus encrypts secrets with
    <data dir>/google_calendar/<user>.json  each user's Google connection
    <data dir>/settings.json                google_oauth_client_id / _secret / _redirect_uri
    the env file                            GOOGLE_OAUTH_CLIENT_ID / _SECRET, when not in settings

and writes, 0600, into the add-on's data folder (<data dir>/mods/google/data
unless --mod-data says otherwise):

    client.json            the same OAuth client (secret re-encrypted with the add-on's key)
    connections/<user>.json  each connection that had Drive, Docs, Sheets, Slides, Tasks
                           or Contacts on (refresh token re-encrypted)

Odysseus keeps its own copy (it still uses it for Google Calendar and Meet):
this is a copy, not a move. It prints counts and product names only, never
a token, a secret or an email address. --dry-run shows what it would do.
An existing connection in the add-on is kept unless --overwrite is given.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Dict, Optional

MOVED = ("drive", "drive_all", "docs", "sheets", "slides", "tasks", "contacts")


def _read_json(path: str) -> Dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def _env_file(path: Optional[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not path or not os.path.exists(path):
        return out
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip().removeprefix("export ").strip()
            if k.startswith("GOOGLE_OAUTH_"):
                out[k] = v.strip().strip('"').strip("'")
    return out


def _core_fernet(data_dir: str):
    from cryptography.fernet import Fernet
    path = os.path.join(data_dir, ".app_key")
    with open(path, "rb") as f:
        return Fernet(f.read().strip())


def _core_decrypt(fernet, value: str) -> str:
    if not value:
        return ""
    if not value.startswith("enc:"):
        return value
    return fernet.decrypt(value[4:].encode()).decode()


def run(data_dir: str, mod_data: str, env_file: Optional[str] = None, dry_run: bool = False,
        overwrite: bool = False, out=sys.stdout) -> Dict:
    data_dir = os.path.abspath(os.path.expanduser(data_dir))
    mod_data = os.path.abspath(os.path.expanduser(mod_data))
    if not os.path.isdir(data_dir):
        raise SystemExit(f"No Odysseus data folder at {data_dir}")
    os.environ["MOD_DATA_DIR"] = mod_data
    from odysseus_google import store as connection  # no httpx needed; its key is in MOD_DATA_DIR

    report = {"client": "", "imported": 0, "skipped": 0, "kept": 0, "products": []}
    core = _core_fernet(data_dir)

    # The OAuth client: settings first, then the env file, as Odysseus reads it.
    settings = _read_json(os.path.join(data_dir, "settings.json"))
    env = {**_env_file(env_file), **{k: v for k, v in os.environ.items() if k.startswith("GOOGLE_OAUTH_")}}
    cid = str(settings.get("google_oauth_client_id") or "").strip() or env.get("GOOGLE_OAUTH_CLIENT_ID", "")
    sec = str(settings.get("google_oauth_client_secret") or "").strip() or env.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
    sec = _core_decrypt(core, sec)
    have = connection.client_config()
    if not (cid and sec):
        report["client"] = "not found in Odysseus (set it up in the add-on's page)"
    elif have["configured"] and not overwrite:
        report["client"] = "kept the add-on's own"
    else:
        report["client"] = "copied"
        if not dry_run:
            connection.save_client(cid, sec, "", by="")

    src_dir = os.path.join(data_dir, "google_calendar")
    names = sorted(n for n in os.listdir(src_dir) if n.endswith(".json")) if os.path.isdir(src_dir) else []
    for name in names:
        d = _read_json(os.path.join(src_dir, name))
        products = [p for p in d.get("products") or [] if p in MOVED]
        if not d.get("refresh_token") or not products:
            report["skipped"] += 1          # Calendar only: nothing for the add-on
            continue
        dest = os.path.join(mod_data, "connections", name)
        if os.path.exists(dest) and not overwrite:
            report["kept"] += 1
            continue
        refresh = _core_decrypt(core, str(d["refresh_token"]))
        report["imported"] += 1
        report["products"].append(products)
        if dry_run:
            continue
        connection._write_json(dest, {
            "email": str(d.get("email") or ""), "refresh_token": connection.encrypt(refresh),
            "scopes": str(d.get("scopes") or ""), "products": products, "checked_at": 0,
            "connected_at": d.get("connected_at") or time.time(), "imported_from_core": time.time()})

    print(("Dry run: " if dry_run else "") + f"OAuth client: {report['client']}.", file=out)
    print(f"Connections {'to import' if dry_run else 'imported'}: {report['imported']}"
          + (f" ({'; '.join(', '.join(p) for p in report['products'])})" if report["products"] else "")
          + f"; already in the add-on, kept: {report['kept']}; Calendar only, skipped: {report['skipped']}.",
          file=out)
    if not dry_run and report["imported"]:
        print("Done. Open the Google add-on: its cards show Connected. Odysseus still has its own copy for "
              "Google Calendar and Meet.", file=out)
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--data-dir", default=os.environ.get("ODYSSEUS_DATA_DIR") or "~/odysseus-data",
                    help="Odysseus's data folder (ODYSSEUS_DATA_DIR)")
    ap.add_argument("--mod-data", default="", help="the add-on's data folder (default <data dir>/mods/google/data)")
    ap.add_argument("--env-file", default="", help="Odysseus's .env, for GOOGLE_OAUTH_CLIENT_ID / _SECRET")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--overwrite", action="store_true", help="replace what the add-on already has")
    a = ap.parse_args(argv)
    data_dir = os.path.expanduser(a.data_dir)
    mod_data = a.mod_data or os.path.join(data_dir, "mods", "google", "data")
    run(data_dir, mod_data, a.env_file or None, a.dry_run, a.overwrite)


if __name__ == "__main__":
    main()
