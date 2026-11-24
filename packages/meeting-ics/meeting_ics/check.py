"""Strict parser and validator for the .ics files this package builds (standard library only).

`parse_ics` reads a feed strictly and raises IcsFormatError on the first problem. `validate` collects every
problem it can find. Both are generic: no jurisdiction, UID scheme or time zone is assumed.
Used by `meeting_ics check-feed FILE` and by the tests.
"""
import datetime
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

MAX_LINE_OCTETS = 75
TEXT_PROPS = ("SUMMARY", "DESCRIPTION", "LOCATION", "COMMENT", "X-WR-CALNAME", "X-WR-CALDESC")
FORBIDDEN_PROPS = ("ATTENDEE", "ORGANIZER")
STATUSES = ("CONFIRMED", "CANCELLED", "TENTATIVE")

_PARAM = r'[A-Za-z0-9-]+=(?:"[^"]*"|[^";:,]*)(?:,(?:"[^"]*"|[^";:,]*))*'
LINE_RE = re.compile(r"^([A-Za-z0-9-]+)((?:;" + _PARAM + r")*):(.*)$", re.DOTALL)
_CONTROL = re.compile("[\x00-\x08\x0a-\x1f\x7f]")
_LOCAL_DT = re.compile(r"^\d{8}T\d{6}$")
_UTC_DT = re.compile(r"^\d{8}T\d{6}Z$")
_OFFSET = re.compile(r"^[+-]\d{4}(\d{2})?$")
_UID = re.compile(r"^\S+@\S+$")


class IcsFormatError(AssertionError):
    """The data is not a well-formed iCalendar stream."""


@dataclass
class Prop:
    name: str
    params: Dict[str, str]
    value: str
    line: int


@dataclass
class Component:
    name: str
    props: List[Prop] = field(default_factory=list)
    children: List["Component"] = field(default_factory=list)
    line: int = 0

    def get(self, name: str) -> Optional[str]:
        """Value of the first property with that name, or None."""
        for p in self.props:
            if p.name == name:
                return p.value
        return None

    def all(self, name: str) -> List[Prop]:
        return [p for p in self.props if p.name == name]

    @property
    def events(self) -> List["Component"]:
        return [c for c in self.children if c.name == "VEVENT"]

    def event(self, uid: str) -> "Component":
        """The VEVENT with that UID; KeyError if there is none."""
        for e in self.events:
            if e.get("UID") == uid:
                return e
        raise KeyError(uid)


def unescape_text(value: str) -> str:
    """Reverse TEXT escaping: backslash-backslash, backslash-semicolon, backslash-comma, backslash-n."""
    out, i = [], 0
    while i < len(value):
        c = value[i]
        if c == "\\" and i + 1 < len(value):
            n = value[i + 1]
            out.append("\n" if n in "nN" else n if n in "\\;," else c + n)
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _split_params(raw: str) -> Dict[str, str]:
    """';A=1;B="x;y"' -> {"A": "1", "B": "x;y"} (names upper-cased, quotes removed)."""
    params, cur, quoted = [], "", False
    for ch in raw:
        if ch == '"':
            quoted = not quoted
        if ch == ";" and not quoted:
            params.append(cur)
            cur = ""
        else:
            cur += ch
    params.append(cur)
    out = {}
    for p in params:
        if p:
            k, _, v = p.partition("=")
            out[k.upper()] = v.replace('"', "")
    return out


def _physical_problems(data: bytes) -> List[str]:
    """Octet-level problems: final CRLF, bare CR or LF, over-long lines, invalid UTF-8."""
    problems = []
    if not data.endswith(b"\r\n"):
        problems.append("file does not end with CRLF")
    if re.search(rb"(?<!\r)\n", data):
        problems.append("bare LF present")
    if re.search(rb"\r(?!\n)", data):
        problems.append("bare CR present")
    lines = data.split(b"\r\n")
    if lines and lines[-1] == b"":
        lines.pop()
    for n, raw in enumerate(lines, 1):
        if len(raw) > MAX_LINE_OCTETS:
            problems.append("line %d is %d octets (>%d)" % (n, len(raw), MAX_LINE_OCTETS))
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError:
            problems.append("line %d is not valid UTF-8" % n)
    return problems


def parse_ics(data: bytes) -> Component:
    """Parse a feed strictly. Returns the single top-level VCALENDAR; raises IcsFormatError otherwise."""
    problems = _physical_problems(data)
    if problems:
        raise IcsFormatError(problems[0])
    physical = data.decode("utf-8").split("\r\n")[:-1]
    # Unfold: a physical line starting with a space or tab continues the previous one.
    logical: List[Tuple[int, str]] = []
    for n, ln in enumerate(physical, 1):
        if ln[:1] in (" ", "\t"):
            if not logical:
                raise IcsFormatError("line %d: continuation line with nothing to continue" % n)
            logical[-1] = (logical[-1][0], logical[-1][1] + ln[1:])
        else:
            logical.append((n, ln))
    root: Optional[Component] = None
    stack: List[Component] = []
    for n, ln in logical:
        if _CONTROL.search(ln):
            raise IcsFormatError("line %d: control character in content line" % n)
        m = LINE_RE.match(ln)
        if not m:
            raise IcsFormatError("line %d: not a valid content line: %r" % (n, ln[:60]))
        name, raw_params, value = m.group(1).upper(), m.group(2), m.group(3)
        if name == "BEGIN":
            comp = Component(value.upper(), line=n)
            if stack:
                stack[-1].children.append(comp)
            elif root is not None:
                raise IcsFormatError("line %d: more than one top-level component" % n)
            else:
                root = comp
            stack.append(comp)
        elif name == "END":
            if not stack or stack[-1].name != value.upper():
                raise IcsFormatError("line %d: END:%s does not match an open BEGIN" % (n, value))
            stack.pop()
        else:
            if not stack:
                raise IcsFormatError("line %d: property outside any component" % n)
            stack[-1].props.append(Prop(name, _split_params(raw_params), value, n))
    if stack:
        raise IcsFormatError("line %d: BEGIN:%s is never closed" % (stack[-1].line, stack[-1].name))
    if root is None or root.name != "VCALENDAR":
        raise IcsFormatError("no top-level VCALENDAR")
    return root


def _text_problems(p: Prop) -> List[str]:
    out, v, i = [], p.value, 0
    while i < len(v):
        c = v[i]
        if c == "\\":
            if i + 1 >= len(v) or v[i + 1] not in "\\;,nN":
                out.append("line %d: invalid escape in %s" % (p.line, p.name))
            i += 2
            continue
        if c in ",;":
            out.append("line %d: unescaped , or ; in TEXT %s" % (p.line, p.name))
        i += 1
    return out


def _walk(comp: Component):
    yield comp
    for c in comp.children:
        yield from _walk(c)


def _once(comp: Component, name: str, errors: List[str], label: str) -> Optional[Prop]:
    found = comp.all(name)
    if len(found) != 1:
        errors.append("%s: expected exactly one %s, found %d" % (label, name, len(found)))
    return found[0] if found else None


def _dt(value: str) -> Optional[datetime.datetime]:
    try:
        return datetime.datetime.strptime(value, "%Y%m%dT%H%M%S")
    except ValueError:
        return None


def validate(data: bytes, expect_events: Optional[int] = None) -> Tuple[List[str], List[str]]:
    """Check a feed. Returns (errors, notes); no errors means the feed is well formed and safe to publish.
    Octet-level problems are all reported, then parsing stops at the first structural problem."""
    errors = _physical_problems(data)
    notes: List[str] = []
    try:
        cal = parse_ics(data)
    except IcsFormatError as e:
        if not errors:
            errors.append(str(e))
        return errors, notes

    for comp in _walk(cal):
        for p in comp.props:
            if p.name in FORBIDDEN_PROPS or "RSVP" in p.params:
                what = p.name if p.name in FORBIDDEN_PROPS else "RSVP"
                errors.append("line %d: %s is forbidden in a public feed (Boundary rule)" % (p.line, what))
            if p.name in TEXT_PROPS:
                errors += _text_problems(p)

    if cal.get("VERSION") != "2.0" or len(cal.all("VERSION")) != 1:
        errors.append("VCALENDAR needs exactly one VERSION:2.0")
    prodid = cal.all("PRODID")
    if len(prodid) != 1 or not prodid[0].value:
        errors.append("VCALENDAR needs exactly one non-empty PRODID")
    for p in cal.all("REFRESH-INTERVAL"):
        if p.params.get("VALUE", "").upper() != "DURATION":
            errors.append("REFRESH-INTERVAL needs VALUE=DURATION (RFC 7986 5.7)")
    if not cal.children:
        errors.append("VCALENDAR has no components (RFC 5545 3.6 requires at least one)")

    zones = set()
    for tz in (c for c in cal.children if c.name == "VTIMEZONE"):
        tzid = _once(tz, "TZID", errors, "VTIMEZONE (line %d)" % tz.line)
        if tzid:
            zones.add(tzid.value)
        label = "VTIMEZONE %s" % (tzid.value if tzid else "?")
        obs = [c for c in tz.children if c.name in ("STANDARD", "DAYLIGHT")]
        if not obs:
            errors.append("%s: needs a STANDARD or DAYLIGHT observance" % label)
        for o in obs:
            for need in ("DTSTART", "TZOFFSETFROM", "TZOFFSETTO"):
                if o.get(need) is None:
                    errors.append("%s: %s (line %d) is missing %s" % (label, o.name, o.line, need))
            for off in ("TZOFFSETFROM", "TZOFFSETTO"):
                v = o.get(off)
                if v is not None and not _OFFSET.match(v):
                    errors.append("%s: %s must look like -0600, got %r" % (label, off, v))

    seen = set()
    events = cal.events
    for ev in events:
        uid_p = _once(ev, "UID", errors, "VEVENT (line %d)" % ev.line)
        uid = uid_p.value if uid_p else "line %d" % ev.line
        for need in ("DTSTAMP", "DTSTART", "SUMMARY"):
            _once(ev, need, errors, uid)
        if uid_p:
            if not _UID.match(uid):
                errors.append("%s: UID must look like local@domain" % uid)
            if uid in seen:
                errors.append("%s: duplicate UID" % uid)
            seen.add(uid)
        stamp = ev.get("DTSTAMP")
        if stamp is not None and not _UTC_DT.match(stamp):
            errors.append("%s: DTSTAMP must be UTC (YYYYMMDDTHHMMSSZ), got %r" % (uid, stamp))
        lm = ev.get("LAST-MODIFIED")
        if lm is not None and not _UTC_DT.match(lm):
            errors.append("%s: LAST-MODIFIED must be UTC (YYYYMMDDTHHMMSSZ), got %r" % (uid, lm))
        status = ev.get("STATUS")
        if status is not None and status not in STATUSES:
            errors.append("%s: STATUS must be one of %s, got %r" % (uid, ", ".join(STATUSES), status))
        seq = ev.get("SEQUENCE")
        if seq is not None and not re.fullmatch(r"\d+", seq):
            errors.append("%s: SEQUENCE must be a number, got %r" % (uid, seq))
        starts = {}
        for pname in ("DTSTART", "DTEND"):
            p = ev.all(pname)[0] if ev.all(pname) else None
            if p is None:
                continue
            tzid = p.params.get("TZID")
            if tzid is None or not _LOCAL_DT.match(p.value):
                errors.append("%s: %s must be a local time with a TZID (not UTC-only or floating), got %r"
                              % (uid, pname, p.value))
                continue
            if tzid not in zones:
                errors.append("%s: %s uses TZID %s, which no VTIMEZONE defines" % (uid, pname, tzid))
            starts[pname] = (tzid, _dt(p.value))
            if starts[pname][1] is None:
                errors.append("%s: %s is not a real date and time: %r" % (uid, pname, p.value))
        if "DTSTART" in starts and "DTEND" in starts:
            (z0, t0), (z1, t1) = starts["DTSTART"], starts["DTEND"]
            if z0 == z1 and t0 and t1 and t1 <= t0:
                errors.append("%s: DTEND is not after DTSTART" % uid)

    if expect_events is not None and len(events) != expect_events:
        errors.append("expected %d events, found %d" % (expect_events, len(events)))
    return errors, notes
