# Google add-on for Odysseus

Google **Drive**, **Docs**, **Sheets**, **Slides**, **Tasks** and **Contacts** for the Odysseus
assistant, on each user's own Google account, asking in the chat before every change.

These used to be built into Odysseus (Settings > Google). They moved into this add-on so the core
stays simple. **Google Calendar and Meet stay in Odysseus**, on its own Google connection
(Settings > Google), together with Sign in with Google and the "ask before writing" gate for Gmail
and Google Calendar.

- [Install](#install)
- [Set up Google](#set-up-google)
- [Carry over an existing connection](#carry-over-an-existing-connection)
- [What the assistant can do](#what-the-assistant-can-do)
- [Ask before writing](#ask-before-writing)
- [Security](#security)
- [Development](#development)

## Install

Settings > Mods > Browse, search **google**, **Review and install** (or Install a mod with
`git@github.com:Jaron-Wilson/odysseus-google.git`). It asks for no permissions. It runs with
Odysseus's own Python (it needs `httpx` and `cryptography`, which Odysseus has; `pypdf` and
`markitdown`, when present, let it read PDFs and Office files from Drive). Turn it on, then open
the **Google** tab (Organize).

## Set up Google

It uses a Google OAuth client from the Google Cloud Console: the one Odysseus already uses for
Sign in with Google and Calendar works.

1. **APIs**: APIs & Services > Library, enable the Google Drive API, Google Docs API, Google Sheets
   API, Google Slides API, Google Tasks API and People API (for the products you want).
2. **Scopes**: Google Auth Platform > Data Access, add:

   | Product | Scope | Google's class |
   | --- | --- | --- |
   | Always | `openid`, `email` | non-sensitive |
   | Drive | `https://www.googleapis.com/auth/drive.file` | non-sensitive |
   | Read all my Drive files | `https://www.googleapis.com/auth/drive.readonly` | restricted |
   | Docs | `https://www.googleapis.com/auth/documents` | sensitive |
   | Sheets | `https://www.googleapis.com/auth/spreadsheets` | sensitive |
   | Slides | `https://www.googleapis.com/auth/presentations` | sensitive |
   | Tasks | `https://www.googleapis.com/auth/tasks` | sensitive |
   | Contacts | `https://www.googleapis.com/auth/contacts.readonly` | sensitive |

3. **Redirect URI**: Credentials > your OAuth client (Web application) > Authorized redirect URIs,
   **add** (keep Odysseus's own `/api/auth/google/callback` too):

   ```
   https://<the address you open Odysseus at>/mods/google/oauth/callback
   ```

   The add-on's page shows the exact value. Behind a proxy that changes the host, set the
   override on the page.
4. **Client**: paste the client ID and secret on the add-on's page (or carry them over, below).
   The first person to save it is the only one who can change it later.
5. **Connect**: press **Connect** on each product card. Google opens in a new tab: keep every box
   ticked, then come back to the Odysseus tab.

Publishing status: In production (Testing drops refresh tokens after 7 days). Google shows an
"unverified app" screen once for your own use.

## Carry over an existing connection

If you used Drive, Docs, Sheets, Slides, Tasks or Contacts in Odysseus before, an admin copies the
OAuth client and everyone's connections into the add-on with one command, so nobody connects
again. In an Odysseus chat (the `!` runs it on the server):

```
! cd ~/odysseus-data/mods/google/src && python3 -m odysseus_google.import_from_core --data-dir ~/odysseus-data --env-file ~/odysseus/.env --dry-run
! cd ~/odysseus-data/mods/google/src && python3 -m odysseus_google.import_from_core --data-dir ~/odysseus-data --env-file ~/odysseus/.env
```

(Your data folder is `ODYSSEUS_DATA_DIR`; `~/odysseus-data` is the usual one.) The first line only
says what it would do. It reads Odysseus's key (`.app_key`), each user's Google connection
(`google_calendar/`), the OAuth client (`settings.json`, else `GOOGLE_OAUTH_CLIENT_ID` and
`GOOGLE_OAUTH_CLIENT_SECRET` in the env file), and writes them, encrypted with the add-on's own key
and mode 0600, into `<data dir>/mods/google/data/`. It **copies**: Odysseus keeps using its copy for
Google Calendar and Meet. It prints counts and product names, never a token, secret or address.
Connections that only had Calendar are skipped; one already in the add-on is kept (add
`--overwrite` to replace it). Any Python with the `cryptography` package works.

You still have to add the add-on's redirect URI (step 3 above) before the next **Connect**; the
carried-over connections work without it.

## What the assistant can do

| Tool | Product | Does | Changes things |
| --- | --- | --- | --- |
| `google_status` | | what is connected, how to connect the rest | no |
| `google_drive_search` | Drive | search or list files (name/text, kind, folder), newest first | no |
| `google_drive_read` | Drive | details plus content as text: Docs as Markdown, Sheets as CSV, Slides as text, PDF/Word/Excel/PowerPoint extracted | no |
| `google_drive_upload` | Drive | upload text, optionally converted to a Doc or Sheet | yes |
| `google_drive_create_folder` | Drive | make a folder | yes |
| `google_doc_read` | Docs | a Doc as Markdown-ish text | no |
| `google_doc_create` | Docs | a new Doc; headings, bullets, lists and bold become Docs formatting | yes |
| `google_doc_edit` | Docs | append, insert, or replace text everywhere | yes |
| `google_sheet_read` | Sheets | tabs, or a range (A1 notation) | no |
| `google_sheet_write` | Sheets | write rows at a range; formulas work | yes |
| `google_sheet_append` | Sheets | add rows after the table | yes |
| `google_sheet_create` | Sheets | a new spreadsheet with tabs and starting rows | yes |
| `google_slides_read` | Slides | a deck's text slide by slide, with speaker notes | no |
| `google_slides_create` | Slides | a deck from an outline | yes |
| `google_tasks_list` | Tasks | task lists, or the tasks in one | no |
| `google_task_create` | Tasks | add a task with notes and a due date | yes |
| `google_contacts_search` | Contacts | look someone up by name, email or phone | no |

Odysseus shows them to the model as `mod__google__<name>` when a message is about Google files
(the keywords in `odysseus-mod.json`). Each call is for the user the assistant works for, on their
own connection. A tool whose product is not connected answers how to connect it. Long results are
cut down with the `offset` or `range` that reads on; creates and edits end with a link to the file.
Uploading a file from the server's disk (the old `path` argument) is gone: upload its text.

## Ask before writing

On by default. Every tool marked "Changes things" is `asks_first`: Odysseus holds the call and the
chat shows it with **Approve** and **Deny**, exactly like a device action, and runs it once when
approved. Reading never asks. The switch is on the add-on's page: anyone can turn it on, only the
person who set up the OAuth client can turn it off.

## Security

- Refresh tokens and the client secret are Fernet-encrypted with `secret.key` in the add-on's data
  folder (0600), never sent to a page or a tool answer, never logged. Access tokens live in memory.
- Pages and tools trust only Odysseus's signed identity headers; each user sees and uses only
  their own connection. A Connect started by one user cannot be finished by another.
- **Disconnect** forgets the connection here but does not revoke it at Google, because with the
  same OAuth client the grant is shared with Odysseus's Google Calendar connection.
  **Disconnect and revoke at Google** revokes it (and then Calendar must be connected again in
  Settings > Google). Odysseus likewise does not revoke when its Calendar is disconnected while
  this add-on is installed.
- Scopes are the narrowest that do the job: Drive starts at `drive.file`, Contacts is read only,
  nothing asks for Gmail.

## Development

```
python -m pytest -q tests          # Google is an httpx.MockTransport; nothing leaves the machine
python -m odysseus_mod.devcore     # run it under a stand-in core
python -m odysseus_google          # standalone (asks for a password)
```

`GOOGLE_ADDON_API_BASE=http://127.0.0.1:PORT` sends every Google call to a fake at
`<base>/<google host>/<path>` (tests and local checks only; an environment variable, never a
setting).

| File | Does |
| --- | --- |
| `odysseus_google/app.py` | The page, its API, Connect and the OAuth callback, the tools |
| `odysseus_google/connection.py` | Per-user connection, consent, tokens, calls to Google |
| `odysseus_google/store.py` | Files, the encryption key, the OAuth client |
| `odysseus_google/products.py` | Products, scopes and tool schemas |
| `odysseus_google/{drive,docs,sheets,slides,tasks,contacts}.py` | One module per API |
| `odysseus_google/import_from_core.py` | Carry Odysseus's connections over |
| `odysseus_mod/` | The Odysseus mod SDK (MIT), from the mod template |

License: AGPL-3.0 (the code came from Odysseus). The `odysseus_mod` SDK is MIT.
