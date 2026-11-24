"""Input contract and validation for meeting records (standard library only).

This module is the only record validator. It collects every problem (up to ERROR_CAP) so a caller can fix an
input file in one pass. Messages are written for the person who owns the input file.

Public API: RecordError, validate_record, validate_records, parse_input, load_input.
"""
import dataclasses
import datetime
import difflib
import functools
import json
import re
from urllib.parse import urlsplit

from .ical import _FIXED, _US_RULE
from .model import LINK_KEYS, REMOTE_KEYS, STATUS_ALIASES, STATUS_CANONICAL, STATUS_EMIT, MeetingRecord

SCHEMA_VERSION = 1
SLUG_RE = r"^[a-z0-9]+(-[a-z0-9]+)*$"                       # max 64
UID_NAMESPACE_RE = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"          # max 128
RECORD_ID_RE = r"^[A-Za-z0-9][A-Za-z0-9._:+=/~-]*$"         # max 256
DATE_RE = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"                   # year 1970..2100
TIME_RE = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"
UTC_STAMP_RE = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]Z$"
TIMEZONE_RE = r"^(UTC|[A-Za-z][A-Za-z0-9_+-]*(/[A-Za-z0-9_+-]+){1,2})$"
END_BASIS_RE = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"              # max 64
URL_RE = r"^https?://[^\s/?#\x00-\x1f\x7f]+[^\s\x00-\x1f\x7f]*$"   # max 2048
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_BAD_URL_CHAR_RE = re.compile(r"[\s\x00-\x1f\x7f]")

REQUIRED = ("uid_namespace", "record_id", "meeting_type", "date", "start_time", "timezone", "status",
            "retrieved_at")  # plus body_name or body
FIELD_ORDER = ("uid_namespace", "record_id", "jurisdiction_slug", "body_slug", "body_name", "body", "meeting_type",
               "date", "start_time", "end_time", "end_basis", "timezone", "status", "location", "remote_access",
               "links", "documents", "retrieved_at", "topics", "provenance")
RECORD_FIELDS = frozenset(FIELD_ORDER)
TOP_KEYS = ("records", "schema_version")
MAX_LEN = {"body_name": 200, "body": 200, "meeting_type": 200, "location": 500, "dial_in": 500, "kind": 64,
           "publish_date": 64, "topic": 500}
ERROR_CAP = 100
MAX_URL = 2048
MAX_OVERNIGHT = datetime.timedelta(hours=12)


class RecordError(ValueError):
    """One validation problem; `.errors` holds every problem found (this one first)."""

    def __init__(self, path, message, record_id=None):
        super().__init__(path, message, record_id)
        self.path = path
        self.message = message
        self.record_id = record_id
        self.errors = [self]

    def line(self):
        """`<path>: <message>[ (record_id '<id>')]`: one problem, as printed by the CLI."""
        s = "%s: %s" % (self.path, self.message)
        if self.record_id is not None:
            s += " (record_id %r)" % self.record_id
        return s

    def __str__(self):
        s = self.line()
        if len(self.errors) > 1 and self.errors[0] is self:
            s += " (+%d more)" % (len(self.errors) - 1)
        return s


def _jtype(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, int):
        return "integer"
    if isinstance(v, float):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, (list, tuple)):
        return "array"
    if isinstance(v, dict):
        return "object"
    return type(v).__name__


def _show(s, limit=60):
    r = repr(s)
    return r if len(r) <= limit else r[:limit - 4] + "...'"


@functools.lru_cache(maxsize=1)
def _available_zones():
    try:
        import zoneinfo
        return frozenset(zoneinfo.available_timezones())
    except Exception:  # no zoneinfo module or no tz data
        return frozenset()


def _builtin_zones():
    return sorted(list(_US_RULE) + list(_FIXED))


class _Rec:
    """Collects the errors of one record and offers the field checkers. Every checker returns the normalized
    value, or None after reporting an error (or for a blank optional value)."""

    def __init__(self, prefix, record_id, errs):
        self.prefix = prefix      # "[3]" or "" when validating one bare record
        self.record_id = record_id
        self.errs = errs

    def path(self, name):
        if not self.prefix:
            return name
        return self.prefix + ("." + name if name else "")

    def err(self, name, message):
        self.errs.append(RecordError(self.path(name), message, self.record_id))

    def text(self, name, v, maxlen, required, multiline=False):
        """Single-line (or \\n-multiline) text."""
        if not isinstance(v, str):
            return self.err(name, "must be a string, got %s" % _jtype(v))
        if v.strip() == "":
            return self.err(name, "must not be blank") if required else None
        if len(v) > maxlen:
            return self.err(name, "longer than %d characters (got %d)" % (maxlen, len(v)))
        for pos, ch in enumerate(v):
            if ch == "\n" and multiline:
                continue
            if ch == "\r" and multiline:
                return self.err(name, "contains a carriage return; use \\n line breaks")
            if _CONTROL_RE.match(ch):
                return self.err(name, "contains a control character (U+%04X) at position %d" % (ord(ch), pos))
        return v

    def pattern(self, name, v, regex, maxlen, what, required=True):
        if not isinstance(v, str):
            return self.err(name, "must be a string, got %s" % _jtype(v))
        if v.strip() == "":
            return self.err(name, "must not be blank") if required else None
        if len(v) > maxlen or not re.fullmatch(regex, v):
            return self.err(name, "%s; got %s" % (what, _show(v)))
        return v

    def url(self, name, v):
        """Optional http(s) URL."""
        if v is None:
            return None
        if not isinstance(v, str):
            return self.err(name, "must be an http(s) URL or null, got %s" % _jtype(v))
        if v.strip() == "":
            return None
        if len(v) > MAX_URL:
            return self.err(name, "URL longer than %d characters" % MAX_URL)
        if not v.startswith(("http://", "https://")) or _BAD_URL_CHAR_RE.search(v):
            return self.err(name, "must be an http(s) URL, got %s" % _show(v))
        try:
            parts = urlsplit(v)
            if not parts.hostname:
                return self.err(name, "URL has no host: %s" % _show(v))
            parts.port  # raises ValueError when out of range or not a number
        except ValueError:
            return self.err(name, "URL has an invalid port: %s" % _show(v))
        if not re.fullmatch(URL_RE, v):
            return self.err(name, "must be an http(s) URL, got %s" % _show(v))
        return v

    def slug(self, name, v):
        if v is None:
            return None
        if not isinstance(v, str):
            return self.err(name, "must be a string or null, got %s" % _jtype(v))
        if v.strip() == "":
            return None
        if len(v) > 64 or not re.fullmatch(SLUG_RE, v):
            hint = re.sub(r"[^a-z0-9]+", "-", v.lower()).strip("-")[:64].strip("-")
            return self.err(name, "must be a slug (lowercase letters, digits, single hyphens, no leading/trailing "
                                  "hyphen, max 64); got %s%s" % (_show(v), " (try %r)" % hint if hint else ""))
        return v

    def record_id_value(self, v):
        if isinstance(v, bool) or not isinstance(v, (str, int)):
            return self.err("record_id", "must be a string or integer, got %s" % _jtype(v))
        if isinstance(v, int):
            if v < 0:
                return self.err("record_id", "integer must be >= 0, got %d" % v)
            v = str(v)
        return self.pattern("record_id", v, RECORD_ID_RE, 256, "may contain only letters, digits and . _ : + = / ~ - "
                            "and must start with a letter or digit (max 256)")

    def date(self, v):
        if not isinstance(v, str):
            return self.err("date", "must be a string, got %s" % _jtype(v))
        if v.strip() == "":
            return self.err("date", "must not be blank")
        if not re.fullmatch(DATE_RE, v):
            return self.err("date", "must be a local date YYYY-MM-DD, got %s" % _show(v))
        try:
            datetime.date(int(v[:4]), int(v[5:7]), int(v[8:]))
        except ValueError:
            return self.err("date", "is not a real calendar date: %s" % _show(v))
        if not 1970 <= int(v[:4]) <= 2100:
            return self.err("date", "year must be between 1970 and 2100, got %s" % v[:4])
        return v

    def time(self, name, v, required):
        if not isinstance(v, str):
            return self.err(name, "must be a string, got %s" % _jtype(v))
        if v.strip() == "":
            return self.err(name, "must not be blank") if required else None
        if not re.fullmatch(TIME_RE, v):
            return self.err(name, "must be a 24-hour local time HH:MM, got %s" % _show(v))
        return v

    def timezone(self, v):
        v = self.pattern("timezone", v, TIMEZONE_RE, 64, "must be an IANA timezone name such as America/Denver")
        if v is None or v in _US_RULE or v in _FIXED:
            return v
        zones = _available_zones()
        if not zones:
            return self.err("timezone", "cannot check timezone %s: this Python has no IANA tz data; install the "
                                        "tzdata package or use one of: %s" % (_show(v), ", ".join(_builtin_zones())))
        if v not in zones:
            near = difflib.get_close_matches(v, sorted(zones), n=1, cutoff=0.8)
            return self.err("timezone", "unknown IANA timezone %s%s"
                            % (_show(v), " (did you mean %r?)" % near[0] if near else ""))
        return v

    def status(self, v):
        if not isinstance(v, str):
            return self.err("status", "must be a string, got %s" % _jtype(v))
        if v not in STATUS_EMIT:
            msg = "unknown status %s; expected one of: %s (also accepted: %s)" % (
                _show(v), ", ".join(STATUS_CANONICAL), ", ".join(STATUS_ALIASES))
            if v.lower() in STATUS_EMIT:
                msg += " (statuses are lowercase: try %r)" % v.lower()
            return self.err("status", msg)
        return v

    def retrieved_at(self, v):
        if not isinstance(v, str):
            return self.err("retrieved_at", "must be a string, got %s" % _jtype(v))
        if not re.fullmatch(UTC_STAMP_RE, v):
            return self.err("retrieved_at", "must be a UTC timestamp YYYY-MM-DDTHH:MM:SSZ, got %s" % _show(v))
        try:
            datetime.datetime.strptime(v, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            return self.err("retrieved_at", "is not a real date/time: %s" % _show(v))
        return v

    def end(self, start, v):
        end = self.time("end_time", v, False)
        if end is None or start is None:
            return end
        s = datetime.datetime.strptime(start, "%H:%M")
        e = datetime.datetime.strptime(end, "%H:%M")
        if e == s:
            return self.err("end_time", "must be after start_time %s (got %s)" % (start, end))
        if e < s and (e + datetime.timedelta(days=1) - s) > MAX_OVERNIGHT:
            return self.err("end_time", "%s is before start_time %s; an end past midnight is accepted only if the "
                                        "meeting lasts at most 12 hours" % (end, start))
        return end

    def obj(self, name, v):
        """Object-or-null container. Returns the dict (possibly None)."""
        if v is not None and not isinstance(v, dict):
            self.err(name, "must be an object or null, got %s" % _jtype(v))
            return None
        return v

    def remote_access(self, v):
        v = self.obj("remote_access", v)
        if v is None:
            return None
        got = {}
        for k in sorted(v, key=str):
            if k not in REMOTE_KEYS:
                self.err("remote_access.%s" % k, "unknown key; allowed: %s" % ", ".join(REMOTE_KEYS))
        got["join_url"] = self.url("remote_access.join_url", v["join_url"]) if "join_url" in v else None
        got["dial_in"] = (self.text("remote_access.dial_in", v["dial_in"], MAX_LEN["dial_in"], False, True)
                          if v.get("dial_in") is not None else None)
        return None if got["join_url"] is None and got["dial_in"] is None else got

    def links(self, v):
        v = self.obj("links", v)
        out = {}
        for k in sorted(v or {}, key=str):
            if k not in LINK_KEYS:
                self.err("links.%s" % k, "unknown link; allowed: %s" % ", ".join(LINK_KEYS))
            else:
                out[k] = self.url("links.%s" % k, v[k])
        return out

    def documents(self, v):
        if v is None:
            return []
        if not isinstance(v, list):
            self.err("documents", "must be an array or null, got %s" % _jtype(v))
            return []
        out = []
        for i, item in enumerate(v):
            base = "documents[%d]" % i
            if not isinstance(item, dict):
                self.err(base, "must be an object, got %s" % _jtype(item))
                continue
            for k in sorted(item, key=str):
                if k not in ("kind", "url", "publish_date"):
                    self.err("%s.%s" % (base, k), "unknown key; allowed: kind, url, publish_date")
            if "kind" not in item:
                self.err(base + ".kind", "required field is missing")
                kind = None
            elif item["kind"] is None:
                self.err(base + ".kind", "required field is null")
                kind = None
            else:
                kind = self.text(base + ".kind", item["kind"], MAX_LEN["kind"], True)
            pd = item.get("publish_date")
            out.append({"kind": kind, "url": self.url(base + ".url", item.get("url")),
                        "publish_date": None if pd is None else self.text(
                            base + ".publish_date", pd, MAX_LEN["publish_date"], False)})
        return out

    def topics(self, v):
        if v is None:
            return None
        if not isinstance(v, list):
            self.err("topics", "must be an array or null, got %s" % _jtype(v))
            return None
        return [self.text("topics[%d]" % i, t, MAX_LEN["topic"], True) for i, t in enumerate(v)]


def _check_record(d, index, errs):
    """Validate one record, appending every problem to errs. Returns the MeetingRecord kwargs (meaningful only
    when no error was appended)."""
    prefix = "[%d]" % index if index is not None else ""
    if not isinstance(d, dict):
        errs.append(RecordError(prefix or "<record>", "record must be an object, got %s" % _jtype(d)))
        return {}
    rid = d.get("record_id")
    rid_ok = (isinstance(rid, int) and not isinstance(rid, bool) and rid >= 0) or (
        isinstance(rid, str) and len(rid) <= 256 and re.fullmatch(RECORD_ID_RE, rid) is not None)
    r = _Rec(prefix, str(rid) if rid_ok else None, errs)
    out = {}

    def get(key, required=True):
        """The value, or None when absent/null (reporting it if required)."""
        if key not in d:
            if required:
                r.err(key, "required field is missing")
            return None
        if d[key] is None:
            if required:
                r.err(key, "required field is null")
            return None
        return d[key]

    # FIELD_ORDER, field by field.
    v = get("uid_namespace")
    out["uid_namespace"] = None if v is None else r.pattern(
        "uid_namespace", v, UID_NAMESPACE_RE, 128, "may contain only letters, digits and . _ - and must start with "
        "a letter or digit (max 128)")
    v = get("record_id")
    out["record_id"] = None if v is None else r.record_id_value(v)
    out["jurisdiction_slug"] = r.slug("jurisdiction_slug", d.get("jurisdiction_slug"))
    out["body_slug"] = r.slug("body_slug", d.get("body_slug"))
    if out["body_slug"] and out["jurisdiction_slug"] is None and not _has_error(errs, r.path("jurisdiction_slug")):
        r.err("body_slug", "set jurisdiction_slug too (a body slug is unique only within a jurisdiction)")
    if "body_name" in d and "body" in d:
        r.err("body", "give body_name or the legacy body, not both")
        out["body"] = None
    else:
        key = "body" if "body" in d else "body_name"
        v = get(key)
        out["body"] = None if v is None else r.text(key, v, MAX_LEN[key], True)
    v = get("meeting_type")
    out["meeting_type"] = None if v is None else r.text("meeting_type", v, MAX_LEN["meeting_type"], True)
    v = get("date")
    out["date"] = None if v is None else r.date(v)
    v = get("start_time")
    out["start_time"] = None if v is None else r.time("start_time", v, True)
    v = get("end_time", False)
    out["end_time"] = None if v is None else r.end(out["start_time"], v)
    v = get("end_basis", False)
    out["end_basis"] = None
    if v is not None:
        basis = r.pattern("end_basis", v, END_BASIS_RE, 64, "must match [A-Za-z0-9][A-Za-z0-9._-]* (max 64)", False)
        if basis is not None and out["end_time"] is None and not _has_error(errs, r.path("end_time")):
            r.err("end_basis", "set only together with end_time")
        elif basis is not None:
            out["end_basis"] = basis
    v = get("timezone")
    out["timezone"] = None if v is None else r.timezone(v)
    v = get("status")
    out["status"] = None if v is None else r.status(v)
    v = get("location", False)
    out["location"] = None if v is None else r.text("location", v, MAX_LEN["location"], False, True)
    out["remote_access"] = r.remote_access(d.get("remote_access"))
    out["links"] = r.links(d.get("links"))
    out["documents"] = r.documents(d.get("documents"))
    v = get("retrieved_at")
    out["retrieved_at"] = None if v is None else r.retrieved_at(v)
    out["topics"] = r.topics(d.get("topics"))
    prov = d.get("provenance")
    if prov is not None and not isinstance(prov, dict):
        r.err("provenance", "must be an object or null, got %s" % _jtype(prov))
        prov = None
    out["provenance"] = dict(prov) if prov else {}

    for k in sorted((k for k in d if k not in RECORD_FIELDS), key=str):
        k = str(k)
        low = k[:-4] if k.endswith("_url") else k
        if low in LINK_KEYS:
            r.err(k, "unknown field (links go in links.%s)" % low)
        else:
            near = difflib.get_close_matches(k, sorted(RECORD_FIELDS), n=1, cutoff=0.75)
            r.err(k, "unknown field%s" % (" (did you mean %r?)" % near[0] if near else ""))
    return out


def _has_error(errs, path):
    return any(e.path == path for e in errs)


def validate_record(d, index):
    """Validate one record dict. Returns normalized MeetingRecord kwargs; raises RecordError (with `.errors`)."""
    errs = []
    out = _check_record(d, index, errs)
    if errs:
        _raise(errs)
    return out


def _raise(errs):
    first = errs[0]
    first.errors = list(errs)
    raise first


def validate_records(items):
    """Validate dicts or MeetingRecords. Returns new MeetingRecords (inputs are never mutated). Empty input,
    duplicate UIDs and per-record problems raise RecordError."""
    items = list(items)
    if not items:
        raise RecordError("<input>", "no records: refusing to build an empty feed")
    errs, out, used = [], [], {}
    for i, item in enumerate(items):
        if isinstance(item, MeetingRecord):
            item = dataclasses.asdict(item)
        before = len(errs)
        kw = _check_record(item, i, errs)
        if len(errs) == before:
            uid = (kw["uid_namespace"], kw["record_id"])
            if uid in used:
                errs.append(RecordError("[%d].record_id" % i, "duplicate UID: uid_namespace %r + record_id %r is "
                                        "already used by record [%d]" % (uid[0], uid[1], used[uid]), uid[1]))
            else:
                used[uid] = i
                out.append(MeetingRecord(**kw))
        if len(errs) >= ERROR_CAP:
            if len(errs) > ERROR_CAP or i < len(items) - 1:
                del errs[ERROR_CAP:]
                errs.append(RecordError("<input>", "too many errors; stopped after %d" % ERROR_CAP))
            break
    if errs:
        _raise(errs)
    return out


def parse_input(data):
    """Top-level shape (an array, or {"records": [...], "schema_version": 1}), then validate_records."""
    if isinstance(data, list):
        return validate_records(data)
    if not isinstance(data, dict):
        raise RecordError("<input>", 'must be a JSON array of records or an object {"records": [...]}, got %s'
                          % _jtype(data))
    errs = [RecordError("<input>.%s" % k, "unknown top-level key; allowed: %s" % ", ".join(TOP_KEYS))
            for k in sorted(data, key=str) if k not in TOP_KEYS]
    if "schema_version" in data:
        sv = data["schema_version"]
        if isinstance(sv, bool) or sv != SCHEMA_VERSION or not isinstance(sv, int):
            errs.append(RecordError("<input>.schema_version", "unsupported schema_version %s; this builder reads %d"
                                    % (_json_text(sv), SCHEMA_VERSION)))
    if "records" not in data:
        errs.append(RecordError("<input>.records", "required field is missing"))
    elif not isinstance(data["records"], list):
        errs.append(RecordError("<input>.records", "must be an array, got %s" % _jtype(data["records"])))
    if errs:
        _raise(errs)
    return validate_records(data["records"])


def _json_text(v):
    try:
        return json.dumps(v)[:40]
    except (TypeError, ValueError):
        return repr(v)[:40]


def _dup_keys(pairs):
    seen = set()
    for k, _ in pairs:
        if k in seen:
            raise RecordError("<input>", "duplicate JSON key %s" % _show(k))
        seen.add(k)
    return dict(pairs)


def _bad_constant(name):
    raise RecordError("<input>", "non-standard JSON constant %s" % name)


def load_input(path):
    """Read a UTF-8 JSON file (BOM tolerated), parse strictly and validate. Raises RecordError."""
    try:
        with open(path, encoding="utf-8-sig") as f:
            text = f.read()
    except UnicodeDecodeError:
        raise RecordError("<input>", "cannot read %s: not valid UTF-8" % _show(path)) from None
    except OSError as e:
        raise RecordError("<input>", "cannot read %s: %s" % (_show(path), e.strerror or e)) from None
    try:
        data = json.loads(text, object_pairs_hook=_dup_keys, parse_constant=_bad_constant)
    except json.JSONDecodeError as e:
        raise RecordError("<input>", "not valid JSON: %s (line %d column %d)" % (e.msg, e.lineno, e.colno)) from None
    return parse_input(data)
