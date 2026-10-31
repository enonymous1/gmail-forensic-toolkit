# Changelog

## 2.1.1 — 2026-10-31

Staff-review fixes (P0) plus a rewritten README. No new commands, no
change to the export formats except the date handling below.

### Fixed

- `calendar upcoming` no longer writes every calendar's events to the
  `primary` CSV: without `--output` the path is now derived from the
  requested calendar id (`exports/calendar/events/<calendar-id>/events.csv`).
- `restore rebuild` no longer aborts the whole rebuild when a payload
  parses but breaks Markdown generation; that report is skipped and
  reported, as the command already promised for unparseable payloads.
- Messages with no parseable Date header are no longer stamped with the
  current time. Their reports say `Date: Unknown` and their export
  filenames use a `nodate` stem, so a filename never implies a date the
  payload did not carry.
- Pagination can no longer loop forever on a repeated page token, and a
  page larger than the requested budget no longer overshoots
  `--max-results`. Gmail message/label listing and Calendar event listing
  now share one pagination helper with both guards.
- A malformed brands JSON file now raises a `ValueError` naming the file
  and the parse error, instead of a bare `JSONDecodeError` traceback. A
  mistyped `--brands` path prints a warning and falls back to the built-in
  brands, instead of falling back silently.

### Added

- `batch process --brands`: batch runs can now use a custom brands file,
  like `archive` and `organize` already could.
- 5 new offline tests covering the fixes above — 56 tests total.

### Changed

- README rewritten: Description, Prerequisites, Installation, Local
  Development, and Deployment sections, corrected export-path and
  `--brands` documentation (the old README claimed `config/brands.json`
  was read by default; it is an example copy of the built-in defaults),
  a Mermaid architecture diagram, and commented placeholders for visuals
  to be captured from a throwaway account or synthetic fixtures — never
  from a real mailbox.

## 2.1.0 — 2026-10-28

Fold-in of the `ss-gmail` project's two capabilities the toolkit lacked:
the Gmail label inventory and Google Calendar exports. Everything else in
`ss-gmail` was already covered here (sender/message fetching, label
application via `batchModify`) or deliberately left behind (its Flask and
Django UIs, its broken imports and hardcoded paths, and its committed
mailbox data). The original `ss-gmail` zip/history must not be pushed.

### Added

- `gmail-toolkit labels inventory`: lists every Gmail label and writes the
  list response to `exports/labels/labels.json`, with a printed table
  including each label's message total.
- `gmail-toolkit labels details`: fetches every label's full detail, writes
  one JSON file per label plus `joined_label_details.json`, and skips (and
  counts) labels whose detail request fails instead of aborting.
- `gmail-toolkit calendar calendars|upcoming|events`: Calendar exports as
  CSV under `exports/calendar/` — every calendar, the next events on one
  calendar (bounded below by now), and every event of one calendar or all
  calendars (`--all`), following every page. Missing event fields become
  empty cells; all-day events use their `date` start/end.
- `calendar.readonly` scope and `get_calendar_service()` in `auth.py`,
  sharing the Gmail credential/token paths and refresh/re-auth handling.
  A token granted before this release needs deleting and a fresh sign-in
  before the first `calendar` command.
- 14 offline tests (fake Calendar service, fake label details, CLI
  end-to-end) — 51 tests total.

### Changed

- Auth internals factored into one `_authenticate()` helper shared by the
  Gmail and Calendar service builders; `get_gmail_service()` behaviour is
  unchanged.
- Project description and keywords now mention the label inventory and
  Calendar exports.

## 2.0.0 — 2026-10-27

Full modernization of the 1.0.0 Gmail Forensic Toolkit.

### Fixed

- **Forensic engine actually runs.** `process_email.py` defined
  `generate_forensic_markdown` and `convert_html_to_markdown` twice; the
  second, placeholder definitions shadowed the forensic ones, so every
  report was hardcoded to Email / Direct (1:1) / 0 tracking with no
  timezone, and dead code sat after a `return`. There is now one definition
  of each, the classifier / scanner / timezone code is called, and the dead
  copy is gone. Verified against a fixture: a meeting/security subject with
  a 1×1 pixel now reports Meeting, 1 pixel, 1 link, and `-0400`.
- **Dependencies match the imports.** `PyYAML` and `python-dateutil` are
  now declared (both were imported but missing from `requirements.txt`, so
  a fresh install failed). Unused `markdownify` is now actually used for
  HTML conversion; `python-dotenv`, `lxml`, `pandocfilters`, and
  `google-auth-httplib2` are dropped. `pandas` is dropped: sender counting
  and CSV output use the standard library.
- **No external `pandoc` binary.** HTML conversion is pure Python via
  `markdownify`, falling back to plain text. The old fallback returned raw
  HTML into Markdown reports on a non-zero pandoc exit.

### Changed

- Installable package: `src/gmail_toolkit/` with a `gmail-toolkit`
  console script and `python -m gmail_toolkit`. Python 3.10+.
- Gmail listing now follows every `nextPageToken` (scrape, archive,
  digest, organise all previously stopped at the first page), retries
  transient 429/5xx errors with backoff, and organising uses
  `batchModify` (one call per 1,000 messages) instead of one `modify` per
  message.
- `organize labels` gains `--dry-run` and `--limit`. Dry-run first: the
  command removes `INBOX` from matched messages.
- Exports are consistently relative to the working directory; archive
  collisions get a numeric suffix instead of overwriting. Plain-text
  sources are now cached alongside JSON and HTML.
- Auth honours `GMAIL_CREDENTIALS_FILE` / `GMAIL_TOKEN_FILE`, recovers
  from corrupt tokens and failed refreshes by re-authenticating, and
  writes the token file with `0600` permissions.
- Branding parses `Name <address>` senders, matches subdomains, and falls
  back to built-in defaults when no brands file is present.
- Classifier counts real recipients for Group scope; timezone extraction
  returns a name or numeric offset instead of a `tzoffset(...)` repr;
  tracking detection handles `1px` dimensions and hidden styles.
- CRLF endings normalised to LF. README rewritten as a landing page;
  the 1.0 README claimed MIT with no LICENSE file, said Python 3.7, and
  used a `yourusername` clone URL.
- Renamed from `ss-emailGmail` to `gmail-forensic-toolkit` before first
  push (repository, package, and README URLs only; the `gmail_toolkit`
  package and `gmail-toolkit` CLI names are unchanged).

### Added

- MIT LICENSE, `pyproject.toml`, hardened `.gitignore` (credentials and
  exports excluded), GitHub Actions CI (Python 3.10–3.12: ruff, mypy,
  pytest), and 37 offline tests covering parsing, classification,
  pagination, retries, CLI commands, and offline restore.

## 1.0.0 — 2026-07-21

Initial commit: Gmail Forensic Toolkit. Typer CLI with scrape, archive,
organise, batch, digest, and restore commands; forensic Markdown reports
with YAML frontmatter; brand-based label hierarchy.
