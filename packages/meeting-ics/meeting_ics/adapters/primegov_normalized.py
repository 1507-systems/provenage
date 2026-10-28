"""Adapter: PrimeGov normalized JSON (meetings.json, {value, provenance, basis} envelopes) -> MeetingRecords.

PrimeGov-specific knowledge (all of it, in one place):
  - fields are {value, provenance, basis} envelopes; unwrap .value
  - record_id is the PrimeGov meeting id; DTSTAMP comes from meeting_id.provenance.retrieved_at
  - the portal link is meeting_url; the normalized file carries no end time (endDateTime is unusable UTC)
  - status vocabulary is already the generic one (cancelled / scheduled_not_cancelled)
"""
import json

from ..model import MeetingRecord


def _v(rec, key):
    x = rec.get(key)
    return x.get("value") if isinstance(x, dict) and "value" in x else x


def convert(data, *, uid_namespace):
    """uid_namespace is required: the caller names the jurisdiction/source namespace used in every UID."""
    out = []
    for rec in data["meetings"]:
        out.append(MeetingRecord(
            uid_namespace=uid_namespace,
            record_id=str(_v(rec, "meeting_id")),
            body=_v(rec, "body"),
            meeting_type=_v(rec, "meeting_type"),
            date=_v(rec, "date"),
            start_time=_v(rec, "start_time"),
            timezone=_v(rec, "timezone"),
            status=_v(rec, "status"),
            location=_v(rec, "location"),
            links={"portal": _v(rec, "meeting_url"), "agenda": _v(rec, "agenda_url"),
                   "packet": _v(rec, "packet_url"), "minutes": _v(rec, "minutes_url"),
                   "video": _v(rec, "video_url")},
            documents=[{"kind": d.get("kind"), "url": (d.get("url") or {}).get("value"),
                        "publish_date": (d.get("publish_date") or {}).get("value")}
                       for d in rec.get("documents", [])],
            retrieved_at=rec["meeting_id"]["provenance"]["retrieved_at"],
            provenance={"source_id": "primegov", "jurisdiction": _v(rec, "jurisdiction")},
        ))
    return out


def load(path, *, uid_namespace):
    with open(path, encoding="utf-8") as f:
        return convert(json.load(f), uid_namespace=uid_namespace)
