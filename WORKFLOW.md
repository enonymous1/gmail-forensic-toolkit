# Workflow: Takeout in, review, plan, apply to the live inbox

For gmail-forensic-toolkit 2.10.1. The whole loop in one place: bring a
Google Takeout in, review and analyze it offline, define an
organization structure from what you found, and apply that structure
to the live Gmail inbox. Detail for each command lives in
[README.md](README.md); this file is the order to run them in.

## 0. Setup (once)

```bash
git clone https://github.com/enonymous1/gmail-forensic-toolkit.git
cd gmail-forensic-toolkit
python -m venv .venv
source .venv/bin/activate   # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -e .
gmail-toolkit --help
gmail-toolkit --version
```

Gmail access needs a Google Cloud project with the Gmail API enabled
(and the Google Calendar API too, if you want the calendar commands),
plus an OAuth2 credential of type **Desktop application**. Download it
as JSON and place it at `credentials/credentials.json` in the directory
you run the CLI from. That directory is git-ignored; never commit it.
The first command you run opens a browser window for sign-in and saves
the token to `credentials/token.json`. Delete that file to
re-authenticate, or if a scope is missing: a stored token only carries
the scopes granted when it was created. Override the locations with
`GMAIL_CREDENTIALS_FILE` and `GMAIL_TOKEN_FILE`.

The scopes, and what each is for: `gmail.readonly` (reading messages),
`gmail.labels` (creating and listing labels), `gmail.modify` (removing
`INBOX` when organising), `calendar.readonly` (calendar commands only).
Only `organize` ever changes labels, and only after you have seen its
`--dry-run` output. Everything else in this guide is read-only until
step 4.

On macOS, do not use the system Python (3.9 — too old). Install
Python 3.10 or newer via Homebrew (`brew install python@3.12`) or the
python.org installer, and use that `python3` to make the venv;
activation is the `source .venv/bin/activate` line above. CI runs the
same gates on Ubuntu, Windows, and macOS.

## 1. Bring the Takeout in

Get the Takeout first: Google Takeout, Mail, `.mbox` format. Then
scope the file, import it, and prove the import completed. The import
is fully offline — no Gmail API call, no OAuth.

```bash
# Scope it: counts, date span, top senders, new vs duplicates, raw size
gmail-toolkit mbox preview ~/Takeout/Mail/"All Mail.mbox"

# Import it, keeping each message's raw bytes beside its report,
# with a journal of every outcome
gmail-toolkit mbox import ~/Takeout/Mail/"All Mail.mbox" --save-eml --log import.jsonl

# Back-check: every message in the file should have a report,
# and every saved .eml should exist with a matching hash
gmail-toolkit mbox verify ~/Takeout/Mail/"All Mail.mbox"
```

What lands where: one forensic Markdown report per message under
`exports/<Brand>/<Sender>/`, plus a `<stem>.eml` beside each report
when `--save-eml` is on. Duplicates are skipped by Message-ID
(messages without one get a deterministic `mbox-<hash>` id, so
re-runs dedupe them); a malformed message is counted and skipped,
never fatal. `--save-eml` is opt-in because it duplicates the `.mbox`
on disk (roughly 1x its size). The `.eml` is the message re-serialized
from the parsed mailbox object, not a byte-exact slice of the `.mbox`
(no `From ` separator line); the recorded hash is of the bytes
actually written, so verify is self-consistent either way. It covers
newly imported messages only — re-running does not backfill `.eml`
files for messages already imported.

Optional, at import time: `--extract-attachments` saves attachment
bytes beside each report in a `<stem>.attachments` directory (also
opt-in; attachments can carry malware and a large Takeout can hold
gigabytes of them).

## 2. Review and analyze, offline

Browse the reports in the read-only local viewer (localhost only,
GET-only, no sign-in inside it), and search the SQLite index:

```bash
gmail-toolkit serve
gmail-toolkit index build
gmail-toolkit index search budgets --brand Example
gmail-toolkit index stats
```

The viewer opens at `http://127.0.0.1:5000` with home counts, a
searchable report list, per-report HTML, and the label and calendar
views. The index (`exports/index.db`) is a derived cache over the
reports — brand, sender, subject, body, recipients, Takeout labels,
threading and authentication headers, attachment info, and the `.eml`
catalog — searchable with FTS5. Deleting it loses nothing; `index
update` rebuilds it. `gmail-toolkit labels inventory` shows the live
account's current labels, for comparison with what the Takeout
carried.

## 3. Define the structure

The structure comes from a brands file plus a plan file. Edit the
brands file first — it maps domains to brands, so it is where the
grouping is decided:

```json
{"example.com": "ExampleBrand", "other.com": "OtherBrand"}
```

```bash
gmail-toolkit organize plan --exports-dir exports --output organize-plan.json
```

The plan is one entry per sender: email, brand, label path, report
count, sorted by email. Brands resolve from the *current* brands
file, not from the reports, so editing the brands file and re-running
`organize plan` reshapes the plan. `--min-count N` leaves out small
senders. Then open `organize-plan.json` and edit it by hand — rename
a branch, merge two brands, drop a sender. The edited file *is* the
structure that gets applied.

## 4. Apply it to the live inbox

```bash
gmail-toolkit organize apply organize-plan.json --dry-run
gmail-toolkit organize apply organize-plan.json --log organize-apply.jsonl
```

Apply creates any missing labels and moves each sender's inbox
messages under its planned label (`Archive-Brands/<Brand>/<email>`
by default), one batch per sender. Run the dry run first — there is
deliberately no confirmation prompt, so the dry run is the guard.
`--keep-inbox` tags messages without removing them from the inbox;
`--limit N` caps messages per sender; `--log` appends one JSON
record per entry plus a summary record, so the outcome survives the
terminal. A failing entry is recorded and skipped, never fatal to
the rest of the plan. Re-running apply is safe: already-moved
messages no longer match `in:inbox`, so they are found zero times.
Check the result with `gmail-toolkit labels inventory`.

## What lives where

| Thing | Location |
|---|---|
| Reports + `.eml` files | `exports/<Brand>/<Sender>/` |
| Search index (derived cache) | `exports/index.db` |
| Import journal | wherever `--log` pointed (`import.jsonl`) |
| Apply journal | wherever `--log` pointed (`organize-apply.jsonl`) |
| Organize plan (the structure) | `organize-plan.json` (or your `--output`) |
| Brands mapping | your brands JSON file, or the built-in defaults |
| OAuth credentials / token | `credentials/credentials.json`, `credentials/token.json` (git-ignored) |

## Limits to know before you start

- **Sender-level, not message-level.** The join key between the
  Takeout and the live inbox is the sender. Your Takeout labels are
  recorded in the reports and the index, but they are not restored
  onto the same live messages — that would need one Message-ID
  search per message, which is prohibitively slow at scale.
- **The Takeout is a snapshot.** Apply queries the live inbox per
  sender, so messages that arrived after the Takeout get organized
  and messages deleted or archived since get skipped. Plan counts
  and live counts will not match exactly; that is what the dry run
  is for.
- **Label budget.** Gmail allows up to 5,000 labels, but Google
  recommends 500 or fewer including nested labels, because more
  than that slows the account down — and every nested label counts
  separately. A per-sender plan with thousands of senders fits the
  hard cap but passes the recommended number; use `--min-count` or
  a brand-level plan to stay inside it.
- **No backfill.** `--save-eml` and `--extract-attachments` cover
  newly imported or archived messages only. Re-importing skips
  existing messages as duplicates, so it will not add `.eml` files
  or attachments to them.
- **The viewer is read-only and local.** It cannot organize
  anything (no buttons, no sign-in, binds `127.0.0.1` only).
  Organizing happens only through the `organize` commands above.
