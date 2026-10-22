"""Google add-on for Odysseus: Drive, Docs, Sheets, Slides, Tasks and
Contacts, as agent tools on the user's own Google account.

    /                       the Google page (tab and settings): account, products, setup
    /connect?products=docs  a page navigation to Google's consent screen
    /oauth/callback         where Google comes back (register it in Google Cloud)
    /api/...                what the page calls (relative URLs)
    /mod/tools              the google_* tools; every write is `asks_first`

Run it under Odysseus (installed as a mod), or on its own with
`python -m odysseus_google` (asks for a password; see the README).
"""
from __future__ import annotations

import asyncio
import html as _html
import json
import logging
import os
from typing import Dict

from odysseus_mod import ModApp, html, json_response, redirect
from odysseus_google import connection
from odysseus_google import products as P
from odysseus_google import tools as T

logger = logging.getLogger(__name__)

MOD_ID = "google"
app = ModApp(mod_id=MOD_ID)


def _run(coro):
    return asyncio.run(coro)


# ── Ask before writing ───────────────────────────────────────────────────

def _prefs_file() -> str:
    return os.path.join(connection.data_dir(), "prefs.json")


def ask_first() -> bool:
    return connection._read_json(_prefs_file()).get("ask_first") is not False


def set_ask_first(on: bool, by: str = "") -> None:
    import time
    connection._write_json(_prefs_file(), {"ask_first": bool(on), "changed_by": by, "changed_at": time.time()})
    _apply_ask_first()
    try:
        app.core.refresh_tools()
    except Exception as e:  # the next start reads them anyway
        logger.info("tool refresh: %s", e)


def _apply_ask_first() -> None:
    on = ask_first()
    for name, (schema, _fn) in app._tools.items():
        schema["x-odysseus"]["asks_first"] = bool(on and P.TOOLS[name]["mutates"])


# ── Tools ────────────────────────────────────────────────────────────────

def _tool_keywords() -> Dict[str, list]:
    out: Dict[str, list] = {n: [] for n in P.TOOLS}
    for phrases, names in P.KEYWORD_HINTS.items():
        for n in names:
            out.setdefault(n, [])
            out[n] += sorted(phrases)
    return {n: sorted(set(k))[:16] for n, k in out.items()}


def _register_tools() -> None:
    kws = _tool_keywords()
    for name, spec in P.TOOLS.items():
        fn = spec["schema"]["function"]
        params = fn["parameters"]

        def handler(req, args, _name=name):
            return _run(T.run_tool(_name, args, req.user))
        app.tool(name, fn["description"], params.get("properties") or {}, params.get("required") or [],
                 keywords=kws.get(name) or [], read_only=not spec["mutates"],
                 asks_first=spec["mutates"])(handler)
    _apply_ask_first()


_register_tools()


# ── The page ─────────────────────────────────────────────────────────────

PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Google</title>
<style>
body{font:14px system-ui,sans-serif;margin:0;padding:18px;background:#111;color:#ddd}
h1{font-size:18px;margin:0 0 4px} h2{font-size:15px;margin:0}
.sub{color:#999;font-size:13px;line-height:1.45}
.card{border:1px solid #2a2a2a;border-radius:8px;padding:12px;margin:12px 0;background:#161616}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(270px,1fr));gap:12px}
.grid .card{margin:0}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:8px}
.pill{font-size:11px;padding:1px 8px;border-radius:10px;border:1px solid #444;color:#aaa}
.pill.on{border-color:#2e7d4f;color:#7fd3a0} .pill.warn{border-color:#9a6a1a;color:#e0b060}
button,a.btn{font:inherit;font-size:13px;padding:4px 12px;border-radius:6px;border:1px solid #444;background:#222;color:#eee;
 text-decoration:none;cursor:pointer} a.btn.primary,button.primary{border-color:#3d6fb8;background:#1d3557}
input[type=text],input[type=password]{font:inherit;font-size:13px;padding:4px 8px;border-radius:6px;border:1px solid #444;
 background:#0d0d0d;color:#eee;min-width:260px}
code{background:#222;padding:0 4px;border-radius:4px;font-size:12px;word-break:break-all}
#msg{min-height:18px;margin-top:6px} .err{color:#f08080}
</style></head><body>
<h1>Google</h1>
<div class="sub">Drive, Docs, Sheets, Slides, Tasks and Contacts on your own Google account, for the assistant.
Google Calendar and Meet are in Odysseus itself (Settings &gt; Google).</div>

<div class="card" id="account">
  <h2>Google account</h2>
  <div class="sub" id="acct">Loading...</div>
  <div class="row" id="acct-actions"></div>
  <div class="sub">Redirect URI for this add-on (add it under Authorized redirect URIs of your OAuth client in
  Google Cloud): <code id="redir2"></code></div>
  <div class="sub" id="msg" role="status"></div>
</div>

<div class="card" id="ask">
  <h2>Ask before writing</h2>
  <div class="row"><label><input type="checkbox" id="askbox"> Ask me in the chat before any change to my Google
  account (create, edit, write, append, upload, new folder, add task)</label></div>
  <div class="sub" id="askstate"></div>
</div>

<div class="grid" id="products"></div>

<div class="card" id="setup" hidden>
  <h2>Set up the Google OAuth client</h2>
  <div class="sub">One free client from the Google Cloud Console (you can use the one Odysseus already uses for
  Sign in with Google and Calendar):</div>
  <ol class="sub">
    <li>APIs &amp; Services &gt; Library: enable the Google Drive, Docs, Sheets, Slides and Tasks APIs and the People API.</li>
    <li>OAuth consent screen &gt; Data Access: add the scopes listed on each card below.</li>
    <li>Credentials &gt; your OAuth client (Web application) &gt; Authorized redirect URIs: add exactly
      <code id="redir"></code></li>
    <li>Paste the client ID and secret here. Or carry everything over from Odysseus with the command in the README.</li>
  </ol>
  <div class="row"><input type="text" id="cid" placeholder="1234-abc.apps.googleusercontent.com" autocomplete="off"></div>
  <div class="row"><input type="password" id="csec" placeholder="Client secret (saved, never shown again)" autocomplete="new-password"></div>
  <div class="row"><input type="text" id="curi" placeholder="Redirect URI override (optional)" autocomplete="off">
    <button class="primary" id="savecid">Save client</button></div>
</div>

<script>
const $ = (id) => document.getElementById(id);
let S = {};
async function api(method, path, body) {
  const r = await fetch(path, {method, headers: body ? {'Content-Type': 'application/json'} : {},
                               body: body ? JSON.stringify(body) : undefined});
  let d = {}; try { d = await r.json(); } catch (_) {}
  if (!r.ok) throw new Error(d.error || ('HTTP ' + r.status));
  return d;
}
function say(t, err) { $('msg').textContent = t || ''; $('msg').className = 'sub' + (err ? ' err' : ''); }
function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function short(s) { return s.replace('https://www.googleapis.com/auth/', ''); }
function connectLink(key, label) {
  return `<a class="btn primary" target="_blank" rel="noopener" href="connect?products=${encodeURIComponent(key)}" data-connect="${esc(key)}">${esc(label)}</a>`;
}
function render(s) {
  S = s;
  const on = s.products.filter(p => p.enabled), lost = s.products.filter(p => p.needs_reconnect);
  $('acct').textContent = !s.configured ? 'Not set up: no Google OAuth client yet (below).'
    : s.connected ? `Connected as ${s.email || 'a Google account'}${on.length ? ': ' + on.map(p => p.label).join(', ') : ''}.`
      + (lost.length ? ` Needs reconnect: ${lost.map(p => p.label).join(', ')}.` : '')
      + (s.imported ? ' (Carried over from Odysseus.)' : '')
    : 'Not connected. Connect a product below.';
  $('acct-actions').innerHTML = s.connected
    ? '<button id="recheck">Recheck</button> <button id="forget">Disconnect</button> <button id="revoke">Disconnect and revoke at Google</button>' : '';
  $('setup').hidden = !!s.configured;
  $('redir').textContent = s.redirect_uri || '';
  $('redir2').textContent = s.redirect_uri || '';
  if (s.client_id && !$('cid').value) $('cid').value = s.client_id;
  $('products').innerHTML = s.products.filter(p => !p.parent).map(p => {
    const kids = s.products.filter(c => c.parent === p.key);
    const pill = p.enabled ? '<span class="pill on">Connected</span>' : p.needs_reconnect ? '<span class="pill warn">Needs reconnect</span>' : '<span class="pill">Not connected</span>';
    let act = !s.configured ? '<span class="sub">Set up the OAuth client first.</span>'
      : p.enabled ? `<button data-off="${esc(p.key)}">Turn off</button>`
      : p.needs_reconnect ? connectLink(p.key, 'Reconnect') + ` <button data-off="${esc(p.key)}">Turn off</button>`
      : connectLink(p.key, p.granted ? 'Turn on' : 'Connect');
    const sub = kids.map(c => `<div class="card"><b>${esc(c.label)}</b> ${c.enabled ? '<span class="pill on">On</span>' : ''}
      <div class="sub">${esc(c.about)}</div><div class="sub">Asks for: ${c.scopes.map(x => '<code>' + esc(short(x)) + '</code>').join(' ')}</div>
      <div class="row">${c.enabled ? `<button data-off="${esc(c.key)}">Turn off</button>` : (s.configured && p.enabled ? connectLink(c.key, 'Allow') : '<span class="sub">Connect ' + esc(p.label) + ' first.</span>')}</div></div>`).join('');
    return `<div class="card" data-product="${esc(p.key)}"><div class="row" style="margin:0;justify-content:space-between"><h2>${esc(p.label)}</h2>${pill}</div>
      <div class="sub">${esc(p.about)}</div>
      <div class="sub">Asks for: ${p.scopes.map(x => '<code>' + esc(short(x)) + '</code>').join(' ')}</div>
      <div class="row">${act}</div>${sub}</div>`;
  }).join('');
  $('askbox').checked = !!s.ask_first;
  $('askbox').disabled = s.ask_first && !s.can_edit_client;
  $('askstate').textContent = s.ask_first ? 'On: every change waits for your Approve in the chat. Reading never asks.'
    : 'Off: the assistant can change your Google files and tasks without asking.';
  $('askstate').className = 'sub' + (s.ask_first ? '' : ' err');
}
async function load() { try { render(await api('GET', 'api/status')); } catch (e) { $('acct').textContent = 'Could not load: ' + e.message; } }
document.addEventListener('click', async (ev) => {
  const t = ev.target;
  try {
    if (t.dataset.connect) { say("Finish on Google's page in the new tab, then come back here."); return; }
    if (t.dataset.off) { render(await api('POST', 'api/products/' + encodeURIComponent(t.dataset.off) + '/disconnect')); say('Turned off.'); }
    else if (t.id === 'recheck') { const r = await api('POST', 'api/recheck'); render(r); say(r.message || (r.checked ? 'Checked with Google just now.' : 'Could not reach Google to check.'), !!r.message); }
    else if (t.id === 'forget') { if (!confirm('Disconnect Google from this add-on? Its tools stop working until you connect again. Google keeps the grant (Odysseus Calendar may share it).')) return; render(await api('POST', 'api/disconnect', {revoke: false})); say('Disconnected.'); }
    else if (t.id === 'revoke') { if (!confirm('Disconnect and ask Google to revoke the access? If Odysseus uses the same OAuth client for Google Calendar, that is revoked too and has to be connected again in Settings > Google.')) return; render(await api('POST', 'api/disconnect', {revoke: true})); say('Disconnected, and Google was asked to revoke the access.'); }
    else if (t.id === 'savecid') { render(await api('POST', 'api/client', {client_id: $('cid').value.trim(), client_secret: $('csec').value.trim(), redirect_uri: $('curi').value.trim()})); $('csec').value = ''; say('Saved. Now connect a product.'); }
  } catch (e) { say(e.message, true); }
});
$('askbox').addEventListener('change', async (ev) => {
  const on = ev.target.checked;
  if (!on && !confirm('Turn off asking before Google changes? The assistant could then create and edit Docs, Sheets, Slides, Drive files and Tasks without asking you.')) { ev.target.checked = true; return; }
  try { render(await api('POST', 'api/ask_first', {enabled: on})); } catch (e) { ev.target.checked = !on; say(e.message, true); }
});
window.addEventListener('focus', load);
load();
</script></body></html>"""


def _view(req) -> Dict:
    out = connection.status(req.user)
    cfg = connection.client_config()
    out["redirect_uri"] = connection.redirect_uri(req.headers, req.base_path)
    out["can_edit_client"] = connection.may_edit_client(req.user)
    if out["can_edit_client"]:
        out["client_id"] = cfg["client_id"]
    out["ask_first"] = ask_first()
    return out


@app.get("/")
def page(req):
    return html(PAGE)


@app.get("/settings")
def settings_page(req):
    return html(PAGE)


@app.get("/api/status")
def api_status(req):
    _run(connection.recheck(req.user))
    return json_response(_view(req))


@app.post("/api/recheck")
def api_recheck(req):
    res = _run(connection.recheck(req.user, force=True))
    out = _view(req)
    out.update(checked=res["checked"], message=connection.lost_message(res["lost"]))
    return json_response(out)


@app.post("/api/products/{product}/disconnect")
def api_product_off(req):
    product = req.params.get("product")
    if product not in P.PRODUCTS:
        return json_response({"error": "No such Google product."}, 404)
    _run(connection.disconnect(req.user, product))
    return json_response(_view(req))


@app.post("/api/disconnect")
def api_disconnect(req):
    body = req.json() if isinstance(req.json(), dict) else {}
    _run(connection.disconnect(req.user, revoke=bool(body.get("revoke"))))
    return json_response(_view(req))


@app.post("/api/client")
def api_client(req):
    if not connection.may_edit_client(req.user):
        return json_response({"error": "Only the person who set up the OAuth client can change it."}, 403)
    b = req.json() if isinstance(req.json(), dict) else {}
    cid = str(b.get("client_id") or "").strip()
    sec = str(b.get("client_secret") or "").strip()
    uri = str(b.get("redirect_uri") or "").strip()
    if not cid.endswith(".apps.googleusercontent.com"):
        return json_response({"error": "A Google OAuth client ID ends in .apps.googleusercontent.com."}, 400)
    if not sec and not connection.client_config()["client_secret"]:
        return json_response({"error": "Paste the client secret too."}, 400)
    if uri and not uri.startswith(("https://", "http://127.0.0.1", "http://localhost")):
        return json_response({"error": "The redirect URI must be https (or a loopback address)."}, 400)
    connection.save_client(cid, sec, uri, by=req.user or "")
    return json_response(_view(req))


@app.post("/api/ask_first")
def api_ask_first(req):
    b = req.json() if isinstance(req.json(), dict) else {}
    on = b.get("enabled") is not False
    if not on and not connection.may_edit_client(req.user):
        return json_response({"error": "Only the person who set up the OAuth client can turn this off."}, 403)
    set_ask_first(on, by=req.user or "")
    return json_response(_view(req))


@app.get("/connect")
def connect(req):
    try:
        url = connection.connect_url(req.user, connection.clean_products(req.query.get("products", "")),
                                     connection.redirect_uri(req.headers, req.base_path))
    except connection.GoogleError as e:
        return _result_page("Could not connect Google", str(e), ok=False)
    return redirect(url, 303)


def _result_page(title: str, message: str, ok: bool):
    color = "#7fd3a0" if ok else "#f08080"
    return html("<!doctype html><meta charset=utf-8><title>Google</title>"
                "<body style='font:15px system-ui;background:#111;color:#ddd;padding:32px;max-width:640px'>"
                f"<h1 style='font-size:19px;color:{color}'>{_html.escape(title)}</h1>"
                f"<p>{_html.escape(message)}</p><p style='color:#999'>You can close this tab.</p></body>",
                200 if ok else 400)


@app.get("/oauth/callback")
def oauth_callback(req):
    q = req.query
    if q.get("error"):
        return _result_page("Google did not connect", f"Google said: {q.get('error')}", ok=False)
    entry = connection.pop_state(q.get("state", ""))
    if not entry:
        return _result_page("Could not connect Google", "That sign-in link expired. Press Connect again.", ok=False)
    if (entry.get("user") or "") != (req.user or ""):
        return _result_page("Could not connect Google", "This connect was started by another user.", ok=False)
    if not q.get("code"):
        return _result_page("Could not connect Google", "Google did not return an authorization code.", ok=False)
    ok, message = _run(connection.finish_connect(req.user, q["code"], entry))
    return _result_page("Google connected" if ok else "Could not connect Google", message, ok=ok)


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    app.run()
