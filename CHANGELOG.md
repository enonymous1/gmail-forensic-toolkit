# Changelog

## 2.10.2 — 2026-11-06

### Fixed

- `test_save_eml_flags_in_help` failed when the environment forces
  colour on (e.g. `FORCE_COLOR=1`): Rich renders `--save-eml` as
  separate styled spans (`-`, `-save`, `-eml`), so the literal flag
  never appears in the raw `--help` output even though the flag is
  there and works. The test now strips ANSI codes before asserting,
  with a regression test that forces colour on and pins both sides
  (raw output lacks the literal flag, stripped output has it). No
  runtime code change.

## 2.10.1 — 2026-11-06

### Changed

- CI now runs on macOS as well as Ubuntu and Windows (matrix
  `ubuntu-latest, windows-latest, macos-latest` × Python 3.10–3.13),
  so the macOS leg is verified by CI on the first run after push.
  No code change.

## 2.10.0 — 2026-11-06

### Added

- Raw `.eml` archive (Takeout + live), opt-in and off by default:
  `mbox import --save-eml`, `archive contact --save-eml`, and
  `batch process --save-eml`. Each newly imported or archived
  message's raw bytes are written beside its report as `<stem>.eml`
  (atomic write, before the report that references it), with the
  filename and SHA-256 in frontmatter (`RawFile`, `RawSha256`), in
  the import journal, and in the index (schema version 4, with
  `eml_path`, `eml_sha256`, `eml_bytes`; older databases auto
  full-rebuild on update, as before). Takeout bytes come from the
  streamed mailbox message; live bytes come from one extra batched
  `format=raw` fetch in the same order (new public
  `gmail_api.decode_raw_message`, which restores the padding Gmail
  omits). `mbox preview` reports total raw files and bytes, so the
  job can be sized first; `mbox verify` checks every saved `.eml`
  exists and its hash matches frontmatter, reporting problems
  separately from missing reports; `restore rebuild` preserves a
  report's `RawFile` link when the `.eml` is still beside it
  (new shared `process_email.read_report_frontmatter`). A failed
  import message leaves no orphaned `.eml` behind, and the journal
  never claims a removed file was saved.

### Deliberately not done

- Backfill for messages already in `exports/` (re-imports skip
  them as duplicates; re-archives would write duplicate reports).
  A backfill pass that re-streams the `.mbox` and writes missing
  `.eml` files is a separate step.
- The viewer does not serve `.eml` files yet; they are files on
  disk beside their reports.
- The Takeout `.eml` is the message re-serialized from the parsed
  mailbox object, not a byte-exact slice of the `.mbox` (no `From `
  separator line); the recorded hash is of the bytes written.

## 2.9.0 — 2026-11-02

### Added

- `gmail-toolkit organize plan`: builds a JSON plan from the analysed
  reports (one entry per sender: email, brand, label path, report
  count, sorted by email). Senders group by bare address parsed from
  frontmatter Sender (display-name variants collapse; reports with no
  `@` address, including "Unknown Sender", are counted as skipped),
  brands resolve from the current brands file rather than frontmatter,
  `.attachments` directories are never scanned, and `--min-count N`
  leaves out small senders.
- `gmail-toolkit organize apply <plan.json>`: applies a whole plan to
  the live inbox — lists labels once (a new label cache, instead of
  one list call per sender as `organize labels` does), creates missing
  labels, and moves each sender's inbox messages with one batchModify
  per sender. `--dry-run` previews the whole plan, `--keep-inbox`
  tags without removing `INBOX`, `--limit` caps messages per sender,
  `--log` appends one JSON record per entry plus a summary record,
  and a failing entry is recorded and skipped rather than aborting
  the plan. Re-running is safe: already-moved messages no longer
  match `in:inbox`. The plan file is human-editable, so the reviewed
  structure — renamed branches, merged brands, dropped senders — is
  what gets applied, including entries whose `label_path` was deleted
  (re-derived from `brand`).
- `gmail_api.create_label` is now public (the `labels().create` call
  with retry); `get_or_create_label` uses it, so the create path is
  defined once.

### Deliberately not done

- Restoring Takeout labels message-by-message (the same message gets
  the same labels it had) is not part of this release: it needs one
  Message-ID search per message, which is prohibitively slow on a
  large mailbox. Sender-level organisation is the practical path.
- No interactive confirmation prompt on apply, by the same rule as
  `organize labels`: the dry run is the guard, so non-interactive
  use keeps working.

## 2.8.0 — 2026-11-01

### Added

- `gmail-toolkit mbox import --extract-attachments`: extracts each
  newly imported message's attachment bytes beside its report in a
  `<stem>.attachments` directory, via atomic writes, with relative
  paths recorded in frontmatter (`AttachmentPaths`) and in the
  index (schema version 3, auto-rebuilt from older schemas on
  update). Off by default; the summary reports files extracted and
  total bytes, and the journal records per-attachment paths and
  whether each was extracted. Filenames are treated as untrusted
  (basename only, illegal characters stripped, Windows-reserved
  stems prefixed, same-name collisions suffixed), and a failed
  message leaves no orphaned files behind.
- `mbox preview` now reports total attachment files and bytes, so
  an extraction job can be sized before running it.
- The index and the viewer skip `*.attachments` directories when
  collecting reports, so an extracted `.md` attachment is never
  indexed or listed as a report.

### Deliberately not done

- Extraction applies to newly imported messages only; re-importing
  does not backfill attachments for messages already imported
  without the flag (delete the report and re-import for one of
  those).
- The API archive path still does not fetch attachment bytes (that
  needs a per-attachment API call) and does not record attachment
  metadata yet; only Takeout imports do.
- `mbox verify` checks reports, not extracted files; the journal
  records their paths for auditing.

## 2.7.0 — 2026-11-01

### Added

- `gmail-toolkit mbox verify <file.mbox>`: a read-only back-check
  that re-streams a Takeout file and reconciles it against the
  exports, reporting verified, in-file duplicates, missing (with
  Message-ID, sender, and subject for each), collisions (same id,
  different body, including fallback-id collisions between distinct
  no-ID messages), and unparseable messages. Re-running an import is
  already safe (dedupe + atomic writes), so resume is just
  re-running it; verify is what proves it finished. Use the same
  `--brands` for verify as for the import.
- `gmail-toolkit mbox import --log <file.jsonl>`: appends one JSON
  record per message outcome (imported, duplicate, or error with the
  error text, plus report path and a full-body hash) and a summary
  record, so errors and skips survive the terminal.
- Full-fidelity frontmatter, for both API and Takeout reports:
  recipients (To, Cc), Gmail labels (names for Takeout, IDs for
  API), threading headers (In-Reply-To, References, Reply-To), and
  Authentication-Results. Takeout reports additionally carry the
  source file and attachment names with type and size.
- Index schema version 2: per-message columns for recipients,
  labels, attachment count and names, source file, and threading and
  authentication headers. A version 1 database is detected as a
  schema mismatch and fully rebuilt on the next `index update`.
  Old-format reports without the new keys index with empty defaults.
- `mbox preview` now also reports the label distribution of the
  file.

### Deliberately not done

- Attachment bytes are still never extracted; only metadata is
  recorded. Extraction to disk, referenced from the database, is a
  separate decision (attachments can carry malware).
- Search does not filter by label yet; labels are stored
  per-message but not yet a search filter or FTS term.
- The API archive path does not record attachment metadata yet;
  only Takeout imports do.

## 2.6.0 — 2026-11-01

### Added

- `gmail-toolkit mbox preview <file.mbox>`: a read-only scan of a
  Takeout file that writes nothing (not even the exports directory).
  Reports message count, date span in UTC, top senders, how many
  messages lack a Message-ID or a parseable date, the error rate, and
  how many messages would be new versus duplicates against the
  existing exports. Parse-only, so faster per message than an import;
  `--limit N` previews just the first N messages.
- `gmail-toolkit mbox import --dry-run`: runs the full import logic
  with writes disabled, reporting what would be imported, skipped as
  duplicates, or skipped as errors. `--quiet` on a real import prints
  only the summary instead of one line per message.
- Dedupe is now one shared implementation for import and preview, and
  the sender-directory path computation is split from the mkdir so
  dry-run and preview never create directories as a side effect.

## 2.5.0 — 2026-11-01

### Added

- SQLite search index over `exports/`, CLI only: `gmail-toolkit index
  build` (full scan into `exports/index.db`, `--db` to override),
  `index search <terms>` (FTS5 over brand, sender, subject, and body,
  best matches first with snippets, `--brand`/`--sender`/`--limit`
  filters), `index stats` (totals, breakdowns by brand/type/scope,
  top senders, tracking totals, date span), and `index update`
  (incremental refresh; a full build when no database exists yet).
  Labels (`labels.json`) and calendar events are indexed alongside
  messages. No new dependencies: `sqlite3` and FTS5 are standard
  library.
- Index design, as decided: the database is a derived cache, never a
  store. Each message row records its report's mtime and size, and an
  update re-scans the directory listing and re-parses only files that
  are new, changed, or gone; deleting the database is always a valid
  recovery. Nothing writes to it at archive/import time, so there is
  exactly one writer and one reader direction. Search terms are
  sanitised to word characters only (including dropping the bare-word
  FTS operators AND/OR/NOT/NEAR), so query input cannot reinterpret
  the FTS query.

### Deliberately not done

- The viewer is NOT wired to the index yet (phase 2, if phase 1 earns
  it). The file-based viewer is untouched.
- No per-message label membership in the index: report frontmatter
  does not carry it, so labels are inventory only.
- No auto-update on archive/import: run `index update` after adding
  reports. A manifest trade-off is documented in the module: a report
  edited in place with identical size and mtime would be missed until
  a full rebuild.

## 2.4.0 — 2026-11-01

### Added

- `gmail-toolkit mbox import <file.mbox>`: import a Google Takeout
  archive as forensic Markdown reports, offline, with no Gmail API call
  or OAuth. The parser is adapted from the companion mbox-explorer
  project (same decoding rules: RFC 2047 headers, charset fallbacks,
  sanitized HTML), but the output flows through the existing forensic
  pipeline — classification, tracking scan, branding, Markdown reports
  — into the same `exports/<Brand>/<Sender>/` layout, so the read-only
  viewer browses imports and API archives alike. Streaming via the
  standard-library `mailbox` module; no new dependencies.
- Import contract: duplicates skipped by Message-ID header (including
  messages already imported by an earlier run); messages without a
  Message-ID get a deterministic `mbox-<hash>` id so re-runs dedupe
  them too; a malformed message is counted and skipped, never fatal.
- The API archive path and the mbox importer now share one
  `get_sender_directory()` helper in `paths.py`, so both layouts stay
  identical by construction.

### Deliberately not done

- Attachment bytes are never extracted on import; only filename, type,
  and size are recorded in the report.
- No `SourceFiles/` cache for mbox imports: the `.mbox` file itself is
  the source of truth, so re-importing is the rebuild path and
  `restore rebuild` does not apply to them.
- The mbox-explorer project itself was NOT folded in or touched: its
  SQLite + FTS5 explorer stays standalone, as decided. Only its parser
  was adapted.

## 2.3.0 — 2026-11-01

### Added

- Read-only local viewer: `gmail-toolkit serve` starts a Flask app at
  `http://127.0.0.1:5000` (`--exports-dir`, `--port`) with home counts,
  a searchable + brand-filtered report list, rendered reports with a
  raw-Markdown view, the label inventory, and calendars + exported
  events. Templates ship in the wheel (`package-data` + `MANIFEST.in`,
  verified by inspecting a built wheel).
- New dependencies, both with major-version upper bounds: Flask and
  Markdown (for rendering report bodies). `types-Markdown` joins the dev
  stubs so `mypy src` stays clean.

### Security design (deliberate)

- Every viewer route is GET-only; there is no POST handler to secure.
- `viewer.py` never imports the auth or API modules, so it cannot call
  Gmail or see the OAuth token. Verified: importing the viewer leaves
  `gmail_toolkit.auth` unimported.
- Report paths are resolved and checked to stay inside the exports
  directory (`..`, absolute paths, and escaping symlinks get a 404).
- The server binds to 127.0.0.1 only and there is no `--host` flag, so
  the viewer cannot be exposed beyond the machine by accident. It uses
  Flask's built-in server: fine for a localhost viewer, not a
  deployment target.

### Deliberately not done

- No run/organize buttons in the viewer: driving Gmail from the browser
  would need a web OAuth flow and a mutation-capable localhost server,
  which is a larger security surface than this release takes on. The
  viewer reads exports; the CLI still does the work.
- No JavaScript or external assets in the templates: server-rendered
  pages only, so the viewer works with no network access.

## 2.2.2 — 2026-11-01

README rendering fixes, spotted on the live repo page. No code change.

### Fixed

- The Mermaid architecture diagram failed to render on GitHub
  ("Parse error ... got 'PS'"). Its node labels were unquoted while
  containing parentheses, and used `\n` line breaks outside quotes —
  both invalid in Mermaid's flowchart grammar. Every label is now quoted
  and breaks use `<br/>`, the documented GitHub-safe form.
- The License badge rendered as literal markdown text instead of an
  image. Its shields URL contained a space
  (`License: MIT-yellow.svg`), and markdown link destinations cannot
  contain spaces. The URL is now the hyphenated form
  (`License-MIT-yellow.svg`).

## 2.2.1 — 2026-10-31

Windows CI fix: the new Windows leg caught a POSIX assumption on its
first run (see below). No behaviour change on POSIX; no command, flag,
or export change anywhere.

### Fixed

- `test_save_token_writes_restricted_file` failed on Windows with mode
  `0o666` (438) instead of `0o600` (384). Cause: on Windows,
  `os.chmod` only toggles the read-only flag and cannot restrict access,
  so `_save_token`'s unconditional `chmod(0o600)` succeeded while
  leaving the file at its default permissions — silently pretending the
  token was owner-only. `_save_token` now chmods on POSIX only, with a
  comment saying why, and the test asserts `0o600` on POSIX only. On
  Windows the token file inherits the parent folder's ACLs, which on a
  normal single-user profile is user-private in practice; a real
  Windows ACL lockdown (pywin32/ctypes) is a separate piece of work,
  deliberately not shipped blind here, since it cannot be verified on
  this box and a wrong DACL could lock the user out of their own token.

## 2.2.0 — 2026-10-31

P1 and remaining staff-review items. No command names, flags, or export
locations changed; one deliberate behaviour change is marked below.

### Added

- Gmail batch fetching: scrape, digest, and archive now fetch messages
  with one batch request per 100 messages instead of one HTTP call per
  message, and label-detail fetching is batched the same way. Services
  without batch support fall back to sequential calls.
- `gmail_toolkit.settings.Settings`: one dataclass for page sizes, batch
  chunk sizes, and retry budgets, accepted by the API wrappers. The CLI
  uses the defaults; embedding callers can pass their own.
- `gmail_toolkit.services` Protocols for the Gmail and Calendar service
  shapes, replacing `service: Any` throughout, so mypy checks the API
  boundary instead of passing it vacuously.
- Testable core functions for every command group
  (`discover_senders`, `summarize_activity`, `organize_sender`,
  `export_inventory`, `export_details`, `export_calendars`,
  `export_upcoming`, `export_events`, `process_batch`), following the
  `archive_contact` pattern: Typer commands now only build the service
  and print, and cores can be called directly with a fake service.
- Export versioning: reports carry a `ToolkitVersion` frontmatter field
  and cached JSON payloads carry a namespaced `toolkit_version` key, so
  a future format change can tell old caches from new ones.
- Global `--verbose` flag (before the subcommand:
  `gmail-toolkit --verbose scrape ...`) enabling debug logging, including
  retry warnings and HTML-conversion fallbacks.
- A test asserting `__init__.py` and `pyproject.toml` agree on the
  version, so the hand-duplicated version cannot drift silently again.

### Changed

- All export writes (reports, cached sources, CSVs, label JSON) are
  atomic: temp file in the same directory plus rename, so a crash can no
  longer leave a truncated file that `restore` chokes on.
- Numeric CLI options are validated: negative `--max-results`,
  `--days`, `--limit`, `--batch-size`, or `--skip` now fail with a usage
  error instead of producing an empty export or an API traceback.
- Retry backoff adds up to 25% jitter, so clients that hit the same
  quota wall do not retry in lockstep.
- Dependencies now carry major-version upper bounds, and CI runs on
  Ubuntu and Windows across Python 3.10–3.13 (the Windows and 3.13 legs
  are verified by CI itself on the first run after push, not locally).
- Header flattening is now one shared helper (`headers_dict`): scrape
  and digest previously re-implemented it, and digest kept only the last
  duplicate header where parse joins duplicates. Behaviour change, in
  the direction of consistency: digest now joins duplicate headers too.
- `classify_message` no longer takes the `sender` argument it explicitly
  ignored (internal API only; reports are unchanged).
- Docstrings now record the deliberate decisions: Meeting-before-
  Notification precedence in classification (changing it is a breaking
  change), the tracking-pixel style heuristic's false-positive risk, and
  the first-`Received`-offset rule for timezones.
- Dead `_LABEL_COLUMNS` removed.

### Fixed

- `fetch_label_details` now skips transport failures (`OSError`) the
  same way it skipped API errors, matching its skip-tolerant claim; a
  per-request batch failure also gets a sequential retry before it is
  skipped for good.

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
