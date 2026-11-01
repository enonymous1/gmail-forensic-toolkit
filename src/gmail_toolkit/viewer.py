"""Read-only local viewer for an ``exports/`` directory.

This module is deliberately isolated from the rest of the toolkit: it
never imports ``auth``, ``gmail_api`` or ``calendar_api``, so it cannot
call the Gmail/Calendar APIs, cannot see the OAuth token, and cannot
modify anything. It only reads files that already exist under the
exports directory and renders them as HTML.

The routes are GET-only by construction (there is no POST handler to
forget about), and the server binds to 127.0.0.1 only — see
``cli/serve.py``, which deliberately offers no ``--host`` flag. Serving
real mailbox content anywhere beyond this machine would break the
project's "nothing leaves the machine" story.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import markdown
import yaml
from flask import Flask, Response, abort, render_template, request

from .paths import iter_report_paths

# Columns shown on the calendar page. Event CSVs come in two shapes
# (full events vs upcoming), so only columns present in a row are shown.
_EVENT_COLUMNS = (
    "Calendar",
    "Event Name",
    "Start Time",
    "End Time",
    "Description",
    "Location",
    "Status",
    "Creator Email",
    "Organizer Email",
    "Attendees",
)


def _resolve_within(root: Path, rel: str) -> Path | None:
    """Resolve *rel* under *root*, or return None if it escapes.

    ``..`` segments, absolute paths, and symlinks pointing outside the
    exports directory are all rejected: the resolved path must stay
    inside the resolved root.
    """
    resolved_root = root.resolve()
    candidate = (root / rel).resolve()
    try:
        candidate.relative_to(resolved_root)
    except ValueError:
        return None
    return candidate


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split a report into its YAML frontmatter dict and body text."""
    if not text.startswith("---\n"):
        return {}, text
    closing = text.find("\n---\n", 4)
    if closing == -1:
        return {}, text
    try:
        data = yaml.safe_load(text[4:closing]) or {}
    except yaml.YAMLError:
        return {}, text
    if not isinstance(data, dict):
        return {}, text
    return data, text[closing + 5 :]


def _list_reports(root: Path) -> list[dict[str, Any]]:
    """Collect every ``*.md`` report under *root* with its frontmatter."""
    reports: list[dict[str, Any]] = []
    if not root.is_dir():
        return reports
    for path in iter_report_paths(root):
        frontmatter, _body = _parse_frontmatter(path.read_text(encoding="utf-8"))
        reports.append(
            {
                "rel": path.relative_to(root).as_posix(),
                "brand": str(frontmatter.get("Brand", "")),
                "sender": str(frontmatter.get("Sender", "")),
                "subject": str(frontmatter.get("Subject", "")),
                "date": str(frontmatter.get("Date", "Unknown")),
                "type": str(frontmatter.get("Type", "")),
                "scope": str(frontmatter.get("Scope", "")),
            }
        )
    # Newest first; reports with no known date go last, never first.
    known = sorted(
        [r for r in reports if r["date"] != "Unknown"],
        key=lambda r: r["date"],
        reverse=True,
    )
    unknown = [r for r in reports if r["date"] == "Unknown"]
    return known + unknown


def _read_labels(root: Path) -> tuple[list[dict[str, Any]], str | None]:
    """Read ``exports/labels/labels.json``; the second value is a warning.

    A missing inventory is a normal empty state (no inventory exported
    yet). A malformed file is not: the warning is shown on the page
    rather than silently presenting an empty table as if it were true.
    """
    path = root / "labels" / "labels.json"
    if not path.exists():
        return [], None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return [], f"Could not read {path}: {exc}"
    if not isinstance(data, list):
        return [], f"Unexpected label inventory shape in {path}: expected a list."
    return [row for row in data if isinstance(row, dict)], None


def _read_calendar(root: Path) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Read ``calendars.csv`` and every ``events.csv`` under ``calendar/``."""
    calendars: list[dict[str, str]] = []
    calendars_path = root / "calendar" / "calendars.csv"
    if calendars_path.exists():
        with calendars_path.open(newline="", encoding="utf-8") as handle:
            calendars = list(csv.DictReader(handle))
    events: list[dict[str, str]] = []
    events_root = root / "calendar" / "events"
    if events_root.is_dir():
        for path in sorted(events_root.rglob("events.csv")):
            with path.open(newline="", encoding="utf-8") as handle:
                for row in csv.DictReader(handle):
                    row = dict(row)
                    row["Calendar"] = path.parent.name
                    events.append(row)
    events.sort(key=lambda row: row.get("Start Time", ""), reverse=True)
    return calendars, events


def create_app(exports_dir: Path | str = Path("exports")) -> Flask:
    """Build the viewer app for *exports_dir* (default ``./exports``)."""
    root = Path(exports_dir)
    app = Flask(__name__)

    @app.get("/")
    def index() -> str:
        reports = _list_reports(root)
        labels, _warning = _read_labels(root)
        _calendars, events = _read_calendar(root)
        return render_template(
            "index.html",
            exports_dir=str(root),
            report_count=len(reports),
            sender_count=len({r["sender"] for r in reports if r["sender"]}),
            brand_count=len({r["brand"] for r in reports if r["brand"]}),
            label_count=len(labels),
            event_count=len(events),
        )

    @app.get("/reports")
    def reports() -> str:
        query = request.args.get("q", "").strip()
        brand = request.args.get("brand", "").strip()
        found = _list_reports(root)
        brands = sorted({r["brand"] for r in found if r["brand"]})
        if brand:
            found = [r for r in found if r["brand"] == brand]
        if query:
            lowered = query.lower()
            found = [
                r
                for r in found
                if lowered
                in " ".join([r["sender"], r["subject"], r["brand"], r["type"]]).lower()
            ]
        return render_template(
            "reports.html",
            exports_dir=str(root),
            reports=found,
            brands=brands,
            query=query,
            brand=brand,
        )

    @app.get("/report/<path:rel>")
    def report(rel: str) -> str:
        path = _resolve_within(root, rel)
        if path is None or not path.is_file() or path.suffix != ".md":
            abort(404)
        frontmatter, body = _parse_frontmatter(path.read_text(encoding="utf-8"))
        html = markdown.markdown(body, extensions=["fenced_code", "tables"])
        return render_template(
            "report.html",
            exports_dir=str(root),
            rel=rel,
            frontmatter=frontmatter,
            body_html=html,
        )

    @app.get("/raw/<path:rel>")
    def raw(rel: str) -> Response:
        path = _resolve_within(root, rel)
        if path is None or not path.is_file() or path.suffix != ".md":
            abort(404)
        return Response(
            path.read_text(encoding="utf-8"), mimetype="text/plain; charset=utf-8"
        )

    @app.get("/labels")
    def labels() -> str:
        rows, warning = _read_labels(root)
        return render_template(
            "labels.html",
            exports_dir=str(root),
            labels=rows,
            warning=warning,
        )

    @app.get("/calendar")
    def calendar() -> str:
        calendars, events = _read_calendar(root)
        return render_template(
            "calendar.html",
            exports_dir=str(root),
            calendars=calendars,
            events=events,
            event_columns=_EVENT_COLUMNS,
        )

    return app
