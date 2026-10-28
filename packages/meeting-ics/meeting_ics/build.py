"""Build a VCALENDAR from MeetingRecords. Rules 1-13: see README.md.
Deterministic: same records + same ledger => identical bytes."""
import datetime
from dataclasses import dataclass, field
from typing import List, Optional

from .ical import esc, fold, ics_utc, vtimezone
from .ledger import Ledger, sha
from .model import LINK_LABELS


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

    @classmethod
    def from_dict(cls, d):
        d = dict(d)
        if "default_duration_minutes" in d:
            d["default_duration"] = datetime.timedelta(minutes=d.pop("default_duration_minutes"))
        if "default_duration_hours" in d:
            d["default_duration"] = datetime.timedelta(hours=d.pop("default_duration_hours"))
        d["end_rules"] = [tuple(r) if isinstance(r, (list, tuple)) else (r["match"], r) for r in d.get("end_rules", [])]
        return cls(**d)


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
    X = opts.x_prefix
    records = list(records)
    zones = {}
    for r in records:
        zones.setdefault(r.timezone, set()).add(int(r.date[:4]))

    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:" + opts.prodid, "CALSCALE:GREGORIAN"]
    if opts.calendar_name:
        lines.append("X-WR-CALNAME:" + esc(opts.calendar_name))
    if len(zones) == 1:
        lines.append("X-WR-TIMEZONE:" + next(iter(zones)))
    for tz in sorted(zones):
        lines += vtimezone(tz, zones[tz])

    events = cancelled = 0
    bumped = []
    seen = set()
    for rec in records:
        uid = "%s-%s@%s" % (rec.uid_namespace, rec.record_id, opts.uid_domain)
        if uid in seen:
            raise ValueError("duplicate UID in input: %s" % uid)
        seen.add(uid)
        tzid = rec.timezone
        stamp = rec.retrieved_at
        body, mtype = rec.body, rec.meeting_type
        status, loc = rec.status, rec.location
        start_dt = datetime.datetime.strptime(rec.date + " " + rec.start_time, "%Y-%m-%d %H:%M")
        end_dt, end_basis = _derive_end(rec, start_dt, opts)

        links = [(label, (rec.links or {}).get(key)) for key, label in LINK_LABELS]
        desc = "\n".join("%s: %s" % (k, u) for k, u in links if u)

        critical = {"body": body, "date": rec.date, "start_time": rec.start_time, "timezone": tzid,
                    "status": status, "location": loc, "meeting_type": mtype,
                    "end_time": end_dt.strftime("%H:%M"), "end_basis": end_basis}
        docs = {"links": [u for _, u in links],
                "documents": sorted([d.get("kind"), d.get("url"), d.get("publish_date")]
                                    for d in rec.documents)}
        led, was_bumped = ledger.update(uid, sha(critical), sha(docs), stamp)
        if was_bumped:
            bumped.append(uid)

        summary = mtype if mtype.lower().startswith(body.lower()) else "%s %s" % (body, mtype)
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
        if status == "cancelled":
            ev.append("STATUS:CANCELLED")
            cancelled += 1
        elif status == "confirmed":
            ev.append("STATUS:CONFIRMED")
        elif status == "scheduled_not_cancelled":
            ev.append("STATUS:CONFIRMED")
            ev.append(X + "-STATUS-BASIS:derived-from-title-not-observed")
        else:
            amb.append("unmapped-status:%s" % status)
        if not portal:
            amb.append("no-portal-meeting-page-url")
        if not loc:
            amb.append("no-location")
        ev += ["SEQUENCE:%d" % led["sequence"], "LAST-MODIFIED:" + ics_utc(led["last_modified"]),
               X + "-DTEND-BASIS:" + end_basis]
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
