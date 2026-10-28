# Gmail Forensic Toolkit

[![CI](https://github.com/enonymous1/gmail-forensic-toolkit/actions/workflows/ci.yml/badge.svg)](https://github.com/enonymous1/gmail-forensic-toolkit/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Archive a Gmail mailbox locally as forensic Markdown, classify what you
archived, and organise it with Gmail labels — from one CLI, with nothing
uploaded anywhere. The same CLI also exports the Gmail label inventory
as JSON and Google Calendar calendars and events as CSV. Every report is
generated on your machine from the Gmail API payload, and the raw JSON,
HTML, and plain-text sources are cached alongside it so reports can be
rebuilt offline later.

## What it does

- **Scrape** a mailbox for unique senders and message counts, written to
  `exports/Contact_Directory.csv`.
- **Archive** every message from a sender as a Markdown report with YAML
  frontmatter: brand, sender, date, timezone, subject, message ID, message
  type (Email / Meeting / Notification / Draft), and scope (Direct / Group /
  Broadcast), plus tracking-pixel and redirect-link counts.
- **Organise** a sender's inbox messages under a hierarchical Gmail label,
  `Archive-Brands/<Brand>/<Sender>`, using `batchModify` — with `--dry-run`
  to see what would move first.
- **Batch-archive** the top senders from the contact directory, a few at a
  time, with resume-by-`--skip`.
- **Digest** recent mailbox activity in a one-line-per-message table.
- **Restore** forensic Markdown from cached JSON payloads, without touching
  the Gmail API at all.
- **Labels** — export the whole label inventory: `labels inventory` writes
  the list response to `exports/labels/labels.json`, and `labels details`
  writes one JSON file per label plus a joined file, with per-label counts.
  Read-only.
- **Calendar** — export Google Calendar data as CSV: `calendar calendars`
  lists every calendar, `calendar upcoming` exports the next events on one
  calendar, and `calendar events --all` exports every event of every
  calendar. Read-only (`calendar.readonly`).

Example report header, generated from a synthetic fixture message (not a
real mailbox):

```markdown
---
Brand: Example
Sender: Fictional Sender <sender@example.com>
Date: '2026-07-21 10:00:00'
Timezone: '-0400'
Subject: 'Security alert: meeting invite'
MessageID: msg-1
Type: Meeting
Scope: Direct (1:1)
---

> [!INFO] **Forensic Classification**
> - **Brand**: Example
> - **Category**: Meeting
> - **Focus**: Direct (1:1)
> - **Timezone**: -0400
> - **Tracking**: Pixels: 1, Links: 1
---
```

## Installation

Requires Python 3.10 or newer.

```bash
git clone https://github.com/enonymous1/gmail-forensic-toolkit.git
cd gmail-forensic-toolkit
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .
```

This installs the `gmail-toolkit` command. `python -m gmail_toolkit` works
the same way.

## Gmail setup (one time)

1. In [Google Cloud Console](https://console.cloud.google.com/), create a
   project and enable the Gmail API. For the calendar commands, enable
   the Google Calendar API too.
2. Create an OAuth2 credential of type **Desktop application** and download
   it as JSON.
3. Place it at `credentials/credentials.json` in the directory you run the
   CLI from. That directory is git-ignored; never commit it.
4. Run any command. A browser window opens for the first sign-in, and the
   token is saved to `credentials/token.json` (written with `0600`
   permissions). Delete that file to re-authenticate.

Override the locations with `GMAIL_CREDENTIALS_FILE` and
`GMAIL_TOKEN_FILE`. Gmail and Calendar share the one token file: a token
granted before the calendar commands existed does not carry the Calendar
scope, so delete `credentials/token.json` and sign in again the first time
you run a `calendar` command.

### Scopes

The tool requests four scopes, and a stored token only carries the scopes
granted when it was created — delete `credentials/token.json` if you change
them:

| Scope | Used for |
|---|---|
| `gmail.readonly` | Reading messages for scrape, archive, and digest |
| `gmail.labels` | Creating the `Archive-Brands/...` labels, and listing labels |
| `gmail.modify` | Removing `INBOX` when organising |
| `calendar.readonly` | Calendar commands only |

Read-only commands never modify anything. Only `organize` changes labels,
and only after you have seen its `--dry-run` output. The label and
calendar commands only read.

## Usage

```bash
# Discover senders (writes exports/Contact_Directory.csv)
gmail-toolkit scrape discovery --max-results 500

# Archive one sender (exports/<Brand>/<Sender>/)
gmail-toolkit archive contact sender@example.com --limit 100

# See what organising would do, then do it
gmail-toolkit organize labels sender@example.com --dry-run
gmail-toolkit organize labels sender@example.com

# Batch-archive the top 10 senders, skipping the first 0
gmail-toolkit batch process --batch-size 10 --skip 0 --limit 100

# Yesterday's activity
gmail-toolkit digest summarize --days 1 --limit 20

# Rebuild reports from cached JSON, offline
gmail-toolkit restore rebuild exports

# Label inventory (read-only)
gmail-toolkit labels inventory
gmail-toolkit labels details

# Calendar exports (read-only; delete credentials/token.json first if your
# stored token predates the Calendar scope)
gmail-toolkit calendar calendars
gmail-toolkit calendar upcoming --max-results 10
gmail-toolkit calendar events --all
```

Run `gmail-toolkit <command> --help` for every option. Exports always land
under `./exports` in the working directory (override with `--exports-dir`
on the archive/batch commands); two messages with the same timestamp and
subject get a numeric suffix instead of overwriting each other.

## Configuration

Edit `config/brands.json` to map sender domains to brand names used for the
export folders and label hierarchy:

```json
{
    "google.com": "Google",
    "microsoft.com": "Microsoft",
    "github.com": "GitHub",
    "apple.com": "Apple",
    "amazon.com": "Amazon"
}
```

Subdomains match their parent domain (`mail.google.com` → Google), and
unmapped senders fall back to `Individual`. Pass `--brands <file>` to use a
different file; when no file is given or it is missing, the built-in
defaults above are used, so the installed CLI works from any directory.

## How it works

- **Pagination and retries.** List calls follow every `nextPageToken`, so a
  sender with more than one page of messages is fully archived. Transient
  Gmail errors (429 / 5xx) are retried with exponential backoff.
- **Forensic parsing.** MIME parts are walked breadth-first, the first text
  body of each type wins, duplicate headers (notably `Received`) are joined
  instead of dropped, and malformed base64 or missing dates degrade
  gracefully instead of crashing the run.
- **HTML to Markdown.** Conversion is pure Python via `markdownify` — no
  external `pandoc` binary is required, and raw HTML is never written into a
  report.
- **Classification.** Message type comes from subject keywords plus the
  Gmail `DRAFT` label; scope comes from list/bulk headers and the actual
  recipient count. Tracking detection counts 1×1 / hidden images and links
  through known redirectors.
- **Labels and Calendar.** Label listing follows every page, detail fetches
  are retried like Gmail calls and a single failed label is skipped rather
  than aborting the inventory. Calendar event listing follows every page;
  upcoming events are bounded below by the current time, and missing event
  fields degrade to empty CSV cells instead of crashing the export.

## Development

```bash
pip install -e ".[dev]"
ruff check src tests
ruff format --check src tests
mypy src
pytest -q
```

All 51 tests run offline against fake Gmail/Calendar services and synthetic
fixtures; no test touches a live Google API or needs credentials. Live
Gmail/Calendar behaviour (OAuth flow, real pagination, label creation) is
the remaining check on your own account after push.

## Changelog

See [CHANGELOG.md](CHANGELOG.md).

## License

MIT — see [LICENSE](LICENSE).
