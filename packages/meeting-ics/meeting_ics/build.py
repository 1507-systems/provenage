"""Build a VCALENDAR from MeetingRecords. Rules 1-13: see README.md.
Deterministic: same records + same ledger => identical bytes."""
import datetime
import re
from dataclasses import dataclass, field, fields
from typing import List, Optional

from .ical import esc, fold, ics_utc, vtimezone
from .ledger import Ledger, sha
from .model import LINK_LABELS, STATUS_EMIT
from .validate import validate_records

_UID_DOMAIN_RE = re.compile(r"[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?")
_X_PREFIX_RE = re.compile(r"X-[A-Za-z0-9-]*[A-Za-z0-9]")
_BASIS_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_DURATION_RE = re.compile(r"P(?!$)(?:(?P<w>[0-9]+)W|(?:(?P<d>[0-9]+)D)?(?:T(?=[0-9])(?:(?P<h>[0-9]+)H)?"
                          r"(?:(?P<m>[0-9]+)M)?(?:(?P<s>[0-9]+)S)?)?)")
_CONTROL_RE = re.compile("[\x00-\x1f\x7f]")
MAX_BASIS = 64


def _duration_seconds(text):
    """Total seconds of an RFC 5545 DURATION (no sign), or None if it is not one."""
    m = _DURATION_RE.fullmatch(text)
    if not m:
        return None
    g = {k: int(v) if v else 0 for k, v in m.groupdict().items()}
    return g["w"] * 604800 + g["d"] * 86400 + g["h"] * 3600 + g["m"] * 60 + g["s"]


@dataclass
class BuildOptions:
    prodid: str = "-//meeting_ics//meeting_ics 0.1//EN"
    uid_domain: str = "example.invalid"
    default_duration: datetime.timedelta = datetime.timedelta(hours=3)
    # list of (meeting_type substring (case-insensitive), rule). rule is "HH:MM" fixed local end, or a
    # timedelta / {"duration_minutes": n} / {"end_local": "HH:MM", "basis": "..."} dict. First match wins.
    end_rules: List = field(default_factory=list)
    x_prefix: str = "X-MEETING-ICS"
    detail: str = "links"          # 'links' implemented; 'topics' and 'full' are stubs
    calendar_name: Optional[str] = None
    method: Optional[str] = "PUBLISH"          # "PUBLISH" or None (omit METHOD)
    refresh_interval: Optional[str] = "PT1H"   # RFC 5545 DURATION or None; drives REFRESH-INTERVAL and X-PUBLISHED-TTL
    calendar_description: Optional[str] = None  # X-WR-CALDESC

    @classmethod
    def from_dict(cls, d):
        """Options from a JSON object. JSON null becomes None. Unknown keys raise ValueError, so a misspelt or
        stray key (a pasted token, say) fails loudly instead of being ignored."""
        d = dict(d)
        if "default_duration_minutes" in d:
            d["default_duration"] = datetime.timedelta(minutes=d.pop("default_duration_minutes"))
        if "default_duration_hours" in d:
            d["default_duration"] = datetime.timedelta(hours=d.pop("default_duration_hours"))
        known = {f.name for f in fields(cls)}
        unknown = sorted(str(k) for k in d if k not in known)
        if unknown:
            raise ValueError("unknown option(s): " + ", ".join(unknown))
        d["end_rules"] = [tuple(r) if isinstance(r, (list, tuple)) else (r["match"], r)
                          for r in (d.get("end_rules") or [])]
        return cls(**d)

    def validate(self):
        """Raise ValueError for an option that could break the feed or inject lines into it."""
        if not isinstance(self.prodid, str) or not self.prodid or _CONTROL_RE.search(self.prodid):
            raise ValueError("prodid must be non-empty text without control characters")
        if not isinstance(self.uid_domain, str) or not _UID_DOMAIN_RE.fullmatch(self.uid_domain):
            raise ValueError("uid_domain must be a host name like example.org, got %r" % (self.uid_domain,))
        if not isinstance(self.x_prefix, str) or not _X_PREFIX_RE.fullmatch(self.x_prefix):
            raise ValueError("x_prefix must look like X-NAME, got %r" % (self.x_prefix,))
        if self.method not in ("PUBLISH", None):
            raise ValueError('method must be "PUBLISH" or null, got %r' % (self.method,))
        if self.refresh_interval is not None:
            secs = (_duration_seconds(self.refresh_interval)
                    if isinstance(self.refresh_interval, str) else None)
            if not secs:
                raise ValueError("refresh_interval must be a positive RFC 5545 duration like PT1H or P1D, got %r"
                                 % (self.refresh_interval,))
        for name in ("calendar_name", "calendar_description"):
            v = getattr(self, name)
            if v is not None and not isinstance(v, str):
                raise ValueError("%s must be text or null" % name)
        for i, item in enumerate(self.end_rules):
            rule = item[1] if isinstance(item, (list, tuple)) and len(item) > 1 else None
            basis = rule.get("basis") if isinstance(rule, dict) else None
            if basis is not None and not (isinstance(basis, str) and len(basis) <= MAX_BASIS
                                          and _BASIS_RE.fullmatch(basis)):
                raise ValueError("end_rules[%d].basis must match [A-Za-z0-9][A-Za-z0-9._-]* (max %d), got %r"
                                 % (i, MAX_BASIS, basis))


def _duration_basis(delta):
    secs = int(delta.total_seconds())
    if secs % 3600 == 0:
        return "default-duration-%dh-assumed" % (secs // 3600)
    return "default-duration-%dm-assumed" % (secs // 60)


def _hm(s):
    return datetime.datetime.strptime(s, "%H:%M")


def _derive_end(rec, start_dt, opts):
    """Returns (end_dt, basis). Precedence: record.end_time > first matching end rule > default duration."""
    if rec.end_time:
        t = _hm(rec.end_time)
        end = start_dt.replace(hour=t.hour, minute=t.minute)
        return (end if end > start_dt else end + datetime.timedelta(days=1)), (rec.end_basis or "source-observed")
    low = (rec.meeting_type or "").lower()
    for match, rule in opts.end_rules:
        if match.lower() not in low:
            continue
        if isinstance(rule, str):
            rule = {"end_local": rule}
        if isinstance(rule, datetime.timedelta):
            rule = {"duration_minutes": rule.total_seconds() / 60}
        if "end_local" in rule:
            t = _hm(rule["end_local"])
            end = start_dt.replace(hour=t.hour, minute=t.minute)
            if end <= start_dt:
                end += datetime.timedelta(days=1)
            return end, rule.get("basis") or "rule-end-%s-local" % rule["end_local"].replace(":", "")
        d = datetime.timedelta(minutes=rule["duration_minutes"])
        return start_dt + d, rule.get("basis") or _duration_basis(d)
    return start_dt + opts.default_duration, _duration_basis(opts.default_duration)


def build_with_report(records, options=None, ledger=None):
    """Returns (ics_bytes, report). report = {events, cancelled, bumped:[uid], ledger}. The ledger (a Ledger,
    default fresh) is updated in place; the caller decides whether to save it."""
    opts = options or BuildOptions()
    ledger = ledger if ledger is not None else Ledger()
    if opts.detail in ("topics", "full"):
        raise NotImplementedError("detail level %r is a stub: topics/full are pending a design decision; "
                                  "only 'links' is implemented" % opts.detail)
    if opts.detail != "links":
        raise ValueError("unknown detail level %r" % opts.detail)
    opts.validate()
    X = opts.x_prefix
    records = validate_records(records)
    zones = {}
    for r in records:
        zones.setdefault(r.timezone, set()).add(int(r.date[:4]))

    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:" + opts.prodid, "CALSCALE:GREGORIAN"]
    if opts.method:
        lines.append("METHOD:" + opts.method)
    if opts.calendar_name:
        lines.append("X-WR-CALNAME:" + esc(opts.calendar_name))
    if opts.calendar_description:
        lines.append("X-WR-CALDESC:" + esc(opts.calendar_description))
    if len(zones) == 1:
        lines.append("X-WR-TIMEZONE:" + next(iter(zones)))
    if opts.refresh_interval:
        lines.append("REFRESH-INTERVAL;VALUE=DURATION:" + opts.refresh_interval)
        lines.append("X-PUBLISHED-TTL:" + opts.refresh_interval)
    for tz in sorted(zones):
        lines += vtimezone(tz, zones[tz])

    events = cancelled = 0
    bumped = []
    for rec in records:
        uid = "%s-%s@%s" % (rec.uid_namespace, rec.record_id, opts.uid_domain)
        tzid = rec.timezone
        stamp = rec.retrieved_at
        body, mtype = rec.body, rec.meeting_type
        status, loc = rec.status, rec.location
        start_dt = datetime.datetime.strptime(rec.date + " " + rec.start_time, "%Y-%m-%d %H:%M")
        end_dt, end_basis = _derive_end(rec, start_dt, opts)

        links = [(label, (rec.links or {}).get(key)) for key, label in LINK_LABELS]
        desc_lines = ["%s: %s" % (k, u) for k, u in links if u]
        ra = rec.remote_access
        if ra:
            if ra.get("join_url"):
                desc_lines.append("Join online: " + ra["join_url"])
            if ra.get("dial_in"):
                desc_lines.append("Dial-in: " + ra["dial_in"])
        desc = "\n".join(desc_lines)

        critical = {"body": body, "date": rec.date, "start_time": rec.start_time, "timezone": tzid,
                    "status": status, "location": loc, "meeting_type": mtype,
                    "end_time": end_dt.strftime("%H:%M"), "end_basis": end_basis}
        if ra is not None:
            critical["remote_access"] = ra
        docs = {"links": [u for _, u in links],
                "documents": sorted(([d["kind"], d["url"], d["publish_date"]] for d in rec.documents),
                                    key=lambda t: [(v is not None, v or "") for v in t])}
        led, was_bumped = ledger.update(uid, sha(critical), sha(docs), stamp)
        if was_bumped:
            bumped.append(uid)

        summary = mtype if mtype.lower().startswith(body.lower()) else "%s %s" % (body, mtype)
        if status == "postponed":
            summary = "Postponed: " + summary
        ev = ["BEGIN:VEVENT", "UID:" + uid, "DTSTAMP:" + ics_utc(stamp),
              "DTSTART;TZID=%s:%s" % (tzid, start_dt.strftime("%Y%m%dT%H%M%S")),
              "DTEND;TZID=%s:%s" % (tzid, end_dt.strftime("%Y%m%dT%H%M%S")),
              "SUMMARY:" + esc(summary)]
        if desc:
            ev.append("DESCRIPTION:" + esc(desc))
        if loc:
            ev.append("LOCATION:" + esc(loc))
        portal = (rec.links or {}).get("portal")
        if portal:
            ev.append("URL:" + portal)
        amb = []
        ical_status, basis, lifecycle = STATUS_EMIT[status]
        ev.append("STATUS:" + ical_status)
        if basis:
            ev.append("%s-STATUS-BASIS:%s" % (X, basis))
        if lifecycle:
            ev.append("%s-LIFECYCLE:%s" % (X, lifecycle))
        if ical_status == "CANCELLED":
            cancelled += 1
        if not portal:
            amb.append("no-portal-meeting-page-url")
        if not loc:
            amb.append("no-location")
        ev += ["SEQUENCE:%d" % led["sequence"], "LAST-MODIFIED:" + ics_utc(led["last_modified"]),
               X + "-DTEND-BASIS:" + esc(end_basis)]
        for a in amb:
            ev.append(X + "-AMBIGUITY:" + esc(a))
        ev.append("END:VEVENT")
        lines += ev
        events += 1
    lines.append("END:VCALENDAR")

    out = "".join(p + "\r\n" for ln in lines for p in fold(ln))
    return out.encode("utf-8"), {"events": events, "cancelled": cancelled, "bumped": bumped, "ledger": ledger}


def build(records, options=None, ledger=None):
    """build(records, options) -> bytes."""
    return build_with_report(records, options, ledger)[0]
