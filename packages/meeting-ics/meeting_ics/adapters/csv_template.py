"""Adapter: the generic CSV template (see `python3 -m meeting_ics template`) -> MeetingRecords.

One row per meeting. Everything the CSV format needs lives here; the shared field rules (dates, URLs, slugs,
timezones, statuses) are enforced by meeting_ics.validate, the same code that checks JSON input.

Rules for the person who owns the file:
  - record_id must never change or be reused. A reschedule keeps the same record_id.
  - To cancel a meeting, set its status to `cancelled`. Do not delete the row: deleting it removes the event
    from every subscriber's calendar.
  - end_time is the published end on the same day; leave it blank to use the end rules or the default duration.
  - join_url is the video-meeting link; dial_in is free text, as published.
  - Header names are exact. A column whose name starts with # is ignored, and so is a row whose record_id starts
    with #. Optional columns may be left out of the header entirely. Blank rows are skipped.
  - Times are 24-hour (18:00; 9:30 and 18:00:00 are also accepted). Dates are YYYY-MM-DD (in Excel format the
    column as Text). Status is case-insensitive.
  - Save as CSV UTF-8 (in Excel: Save As > CSV UTF-8). The comma is the only delimiter.
  - retrieved_at (UTC, YYYY-MM-DDTHH:MM:SSZ) may be filled per row, or given once for the whole file with
    --retrieved-at on the command line (or the retrieved_at argument here).

Errors are RecordError with row-based paths (`row 3.date`, `row 3` for a whole record, `<header>`, `<input>`);
every problem found is reported in one pass (up to validate.ERROR_CAP).
"""
import csv
import io
import re

from ..model import LINK_KEYS, MeetingRecord
from ..validate import ERROR_CAP, RecordError, _Rec, validate_record

TEMPLATE_COLUMNS = ("record_id", "jurisdiction_slug", "body_slug", "body_name", "meeting_type", "date", "start_time",
                    "end_time", "timezone", "status", "location", "join_url", "dial_in", "portal_url", "agenda_url",
                    "packet_url", "minutes_url", "video_url", "retrieved_at")
REQUIRED_COLUMNS = ("record_id", "jurisdiction_slug", "body_slug", "body_name", "meeting_type", "date", "start_time",
                    "timezone", "status")
_URL_COLUMNS = {key + "_url": key for key in LINK_KEYS}   # portal_url -> portal
_SHORT_TIME = re.compile(r"^([0-9]):([0-5][0-9])(:00)?$")
_LONG_TIME = re.compile(r"^([01][0-9]|2[0-3]):([0-5][0-9]):00$")
_NOT_UTF8 = "is not UTF-8; in Excel use Save As > CSV UTF-8"


def _norm_time(v):
    """H:MM -> HH:MM, HH:MM:00 -> HH:MM. Anything else is returned unchanged for validate to judge."""
    m = _SHORT_TIME.match(v)
    if m:
        return "0%s:%s" % (m.group(1), m.group(2))
    m = _LONG_TIME.match(v)
    return v[:5] if m else v


def _check_retrieved_at(v):
    errs = []
    _Rec("", None, errs).retrieved_at(v)
    if errs:
        raise RecordError("--retrieved-at", errs[0].message)


def _field_path(path):
    """Map validate's record field path to the CSV column the person edits."""
    if path.startswith("links."):
        return path[len("links."):] + "_url"
    if path.startswith("remote_access."):
        return path[len("remote_access."):]
    return path


def _row_path(n, path):
    if path in ("", "<record>"):
        return "row %d" % n
    return "row %d.%s" % (n, _field_path(path))


def _header(names):
    """-> (errors, columns) where columns is a list of canonical names (None for an ignored column)."""
    errs, cols, seen = [], [], set()
    unknown = []
    for raw in names:
        name = raw.strip()
        if name == "" or name.startswith("#"):
            cols.append(None)
            continue
        if name in seen:
            errs.append(RecordError("<header>", "duplicate column %r" % name))
        seen.add(name)
        if name not in TEMPLATE_COLUMNS:
            unknown.append(name)
        cols.append(name)
    if unknown:
        errs.append(RecordError("<header>", "unknown column(s): %s (prefix a column with # to ignore it)"
                                % ", ".join(repr(u) for u in unknown)))
    missing = [c for c in REQUIRED_COLUMNS if c not in seen]
    if missing:
        errs.append(RecordError("<header>", "missing required column(s): %s (see: meeting_ics template)"
                                % ", ".join(missing)))
    return errs, cols


def convert(text, *, uid_namespace, retrieved_at=None):
    """CSV text -> list of MeetingRecord. Raises RecordError carrying every problem found."""
    if retrieved_at is not None:
        _check_retrieved_at(retrieved_at)
    if text.startswith("﻿"):
        text = text[1:]
    errs = []
    reader = csv.reader(io.StringIO(text, newline=""))
    records, rows = [], {}
    cols, ignored_named = None, set()
    prev_end = 0
    try:
        for cells in reader:
            start = prev_end + 1
            prev_end = reader.line_num
            if cols is None:
                if not any(c.strip() for c in cells):
                    continue  # blank lines above the header
                herrs, cols = _header(cells)
                ignored_named = {i for i, raw in enumerate(cells) if raw.strip().startswith("#")}
                if herrs:
                    raise _collect(herrs)
                continue
            _convert_row(cells, cols, ignored_named, start, uid_namespace, retrieved_at, rows, records, errs)
            if len(errs) > ERROR_CAP:
                break
    except csv.Error as e:
        errs.append(RecordError("row %d" % (prev_end + 1) if cols else "<input>", "is not valid CSV: %s" % e))
    if cols is None and not errs:
        raise RecordError("<header>", "the file is empty; expected a header row (see: meeting_ics template)")
    if errs:
        raise _collect(errs)
    return records


def _collect(errs):
    if len(errs) > ERROR_CAP:
        errs = errs[:ERROR_CAP] + [RecordError("<input>", "too many errors; stopped after %d" % ERROR_CAP)]
    first = errs[0]
    first.errors = list(errs)
    return first


def _convert_row(cells, cols, ignored_named, n, uid_namespace, retrieved_at, rows, records, errs):
    if not any(c.strip() for c in cells):
        return
    d = {}
    for i, col in enumerate(cols):
        cell = cells[i].replace("\r\n", "\n").replace("\r", "\n").strip() if i < len(cells) else ""
        if col is None:
            if cell and i not in ignored_named:
                errs.append(RecordError("row %d" % n, "has a value under the unnamed column %d" % (i + 1)))
            continue
        d[col] = cell or None
    if len(cells) > len(cols) and any(c.strip() for c in cells[len(cols):]):
        errs.append(RecordError("row %d" % n, "has more cells than the header has columns"))
    if (d.get("record_id") or "").startswith("#"):
        return
    before = len(errs)
    blank = set()
    for col in REQUIRED_COLUMNS:
        if d.get(col) is None:
            errs.append(RecordError("row %d.%s" % (n, col), "is required"))
            blank.add(col)
    if d.get("status"):
        s = d["status"].lower()
        d["status"] = "cancelled" if s == "canceled" else s
    for col in ("start_time", "end_time"):
        if d.get(col):
            d[col] = _norm_time(d[col])
    if d.get("retrieved_at") is None:
        if retrieved_at is not None:
            d["retrieved_at"] = retrieved_at
        else:
            errs.append(RecordError("row %d.retrieved_at" % n, "is blank and --retrieved-at was not given"))
            blank.add("retrieved_at")
    rid = d.get("record_id")
    if rid is not None:
        if rid in rows:
            errs.append(RecordError("row %d.record_id" % n, "duplicates record_id %r on row %d" % (rid, rows[rid]),
                                    rid))
        else:
            rows[rid] = n
    rec = {"uid_namespace": uid_namespace, "provenance": {"adapter": "csv_template", "row": n}}
    for key in ("record_id", "jurisdiction_slug", "body_slug", "body_name", "meeting_type", "date", "start_time",
                "end_time", "timezone", "status", "location", "retrieved_at"):
        if d.get(key) is not None:
            rec[key] = d[key]
    links = {k: d[c] for c, k in _URL_COLUMNS.items() if d.get(c) is not None}
    if links:
        rec["links"] = links
    remote = {k: d[k] for k in ("join_url", "dial_in") if d.get(k) is not None}
    if remote:
        rec["remote_access"] = remote
    try:
        kw = validate_record(rec, None)
    except RecordError as e:
        for pe in e.errors:
            if pe.path in blank:
                continue  # already reported as "is required"
            errs.append(RecordError(_row_path(n, pe.path), pe.message, rid if pe.record_id is not None else None))
        return
    if len(errs) == before:
        records.append(MeetingRecord(**kw))


def load(path, *, uid_namespace, retrieved_at=None):
    """Read a CSV file (UTF-8, BOM tolerated) -> list of MeetingRecord. Raises RecordError."""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as e:
        raise RecordError("<input>", "cannot read the file: %s" % (e.strerror or e)) from None
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise RecordError("<input>", _NOT_UTF8) from None
    return convert(text, uid_namespace=uid_namespace, retrieved_at=retrieved_at)
