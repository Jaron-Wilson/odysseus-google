"""The agent's google_* tools (names, schemas and products in products.py).

Every call is for the user the agent works for (the signed identity) and
their own connection in this add-on. A tool whose product is off answers
how to connect it instead of running.
"""
from __future__ import annotations

import logging
from typing import Dict, Optional

from odysseus_google import connection
from odysseus_google import products as P
from odysseus_google.common import link_line
from odysseus_google.connection import GoogleError

logger = logging.getLogger(__name__)


def status_text(owner: Optional[str]) -> str:
    st = connection.status(owner)
    if not st["configured"]:
        return ("No Google OAuth client is set up in the Google add-on yet, so no Google product can be "
                "connected. Open the Google add-on's settings (Workspace tab Google, Settings) and follow "
                "the setup steps.")
    on = [p for p in st["products"] if p["enabled"]]
    lost = [p for p in st["products"] if p.get("needs_reconnect")]
    off = [p for p in st["products"] if not p["enabled"] and not p.get("needs_reconnect")]
    lines = []
    if on:
        lines.append(f"Connected as {st['email'] or 'a Google account'}: " + ", ".join(p["label"] for p in on) + ".")
    else:
        lines.append("No Google product is connected.")
    if lost:
        lines.append("Needs reconnect (Google no longer holds the permission): "
                     + ", ".join(p["label"] for p in lost) + ". The user presses Reconnect on its card in the "
                     "Google add-on and keeps every box ticked on Google's consent screen.")
    if off:
        lines.append("Not connected: " + ", ".join(p["label"] for p in off)
                     + ". The user turns them on with Connect on each card in the Google add-on.")
    lines.append("Google Calendar and Meet are in Odysseus itself (Settings > Google).")
    return " ".join(lines)


async def _dispatch(tool: str, a: Dict, owner: Optional[str]) -> Dict:
    from odysseus_google import contacts, docs, drive, sheets, slides, tasks
    if tool == "google_drive_search":
        return await drive.search(owner, a.get("query") or "", a.get("kind") or "", a.get("folder_id") or "",
                                  a.get("limit") or 20, a.get("page_token") or "")
    if tool == "google_drive_read":
        return await drive.read(owner, a.get("file") or a.get("file_id") or a.get("id") or "",
                                a.get("offset") or 0, a.get("max_chars"), bool(a.get("details_only")))
    if tool == "google_drive_upload":
        if a.get("content") is None:
            raise GoogleError("Pass content (the file's text).")
        convert = str(a.get("convert") or "").lower()
        return await drive.upload(owner, a.get("name") or "", str(a.get("content")).encode("utf-8"),
                                  a.get("mime_type") or "", a.get("folder_id") or "",
                                  convert if convert in ("doc", "sheet") else "")
    if tool == "google_drive_create_folder":
        return await drive.create_folder(owner, a.get("name") or "", a.get("parent_id") or "")
    if tool == "google_doc_read":
        return await docs.read(owner, a.get("document") or a.get("id") or "", a.get("offset") or 0, a.get("max_chars"))
    if tool == "google_doc_create":
        return await docs.create(owner, a.get("title") or "", a.get("content") or "")
    if tool == "google_doc_edit":
        return await docs.edit(owner, a.get("document") or a.get("id") or "", a.get("action") or "",
                               a.get("text") or "", a.get("index"), a.get("find") or "", a.get("replace") or "",
                               a.get("match_case", True) is not False)
    if tool == "google_sheet_read":
        return await sheets.read(owner, a.get("spreadsheet") or a.get("id") or "", a.get("range") or "",
                                 bool(a.get("formulas")), a.get("max_rows") or 100)
    if tool == "google_sheet_write":
        return await sheets.write(owner, a.get("spreadsheet") or a.get("id") or "", a.get("range") or "",
                                  a.get("rows") or a.get("values"))
    if tool == "google_sheet_append":
        return await sheets.append(owner, a.get("spreadsheet") or a.get("id") or "", a.get("range") or "",
                                   a.get("rows") or a.get("values"))
    if tool == "google_sheet_create":
        return await sheets.create(owner, a.get("title") or "", a.get("tabs"), a.get("rows"))
    if tool == "google_slides_read":
        return await slides.read(owner, a.get("presentation") or a.get("id") or "", a.get("offset") or 0,
                                 a.get("max_chars"))
    if tool == "google_slides_create":
        return await slides.create(owner, a.get("title") or "", a.get("subtitle") or "", a.get("slides"))
    if tool == "google_tasks_list":
        return await tasks.list_tasks(owner, a.get("tasklist") or "", bool(a.get("show_completed")),
                                      a.get("limit") or 50)
    if tool == "google_task_create":
        return await tasks.create(owner, a.get("title") or "", a.get("notes") or "", a.get("due") or "",
                                  a.get("tasklist") or "")
    if tool == "google_contacts_search":
        return await contacts.search(owner, a.get("query") or "", a.get("limit") or 10)
    raise GoogleError(f"Unknown Google tool {tool}.")


async def run_tool(tool: str, args: Dict, owner: Optional[str] = None) -> Dict:
    """{"output": text, "data": {...}} or {"error": text}, the mod tool answer."""
    spec = P.TOOLS.get(tool)
    if not spec:
        return {"error": f"Unknown Google tool {tool}."}
    if tool == "google_status":
        return {"output": status_text(owner)}
    product = spec["product"]
    if not connection.is_enabled(owner, product):
        label = P.PRODUCTS[product]["label"]
        return {"error": f"{label} is not connected. The user can connect it in the Google add-on's settings."}
    try:
        res = await _dispatch(tool, args if isinstance(args, dict) else {}, owner)
    except GoogleError as e:
        return {"error": str(e)}
    if res.get("error"):
        return {"error": str(res["error"])}
    out = str(res.get("output") or "Done.")
    link = res.get("google_link")
    if isinstance(link, dict):
        # The link goes into the answer, so the user always gets it.
        out += link_line(link)
    data = {k: v for k, v in res.items() if k not in ("output", "exit_code")}
    return {"output": out, "data": data}
