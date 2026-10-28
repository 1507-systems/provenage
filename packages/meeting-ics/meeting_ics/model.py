"""Source-agnostic input record for the builder.

A MeetingRecord is plain data; an adapter produces them from any upstream. Fields:

  uid_namespace  str   Prefix naming the source + jurisdiction, e.g. "examplegov-springfield". Together with
                       record_id and the uid_domain option it forms the stable UID
                       "<uid_namespace>-<record_id>@<uid_domain>".
  record_id      str   Identifier unique within the namespace (stable across re-fetches).
  body           str   Governing body name, e.g. "City Council".
  meeting_type   str   Type/title text, e.g. "City Council Regular Session". Used for SUMMARY and end rules.
  date           str   Local date "YYYY-MM-DD".
  start_time     str   Local start "HH:MM".
  timezone       str   IANA zone name, e.g. "America/Denver".
  status         str   "cancelled" -> STATUS:CANCELLED; "confirmed" -> STATUS:CONFIRMED (observed);
                       "scheduled_not_cancelled" -> CONFIRMED plus a STATUS-BASIS note (derived, not observed);
                       anything else -> no STATUS and an AMBIGUITY note.
  location       str|None
  links          dict  Any of portal, agenda, packet, minutes, video (str or None). Missing/None are omitted.
  documents      list  [{"kind": str, "url": str|None, "publish_date": str|None}] (flat strings). Only feeds the
                       change hash (revision detection), never emitted.
  retrieved_at   str   UTC fetch time "YYYY-MM-DDTHH:MM:SSZ"; becomes DTSTAMP (never the wall clock).
  end_time       str|None  Optional observed local end "HH:MM" (same day). Beats end rules and the default.
  end_basis      str|None  Label for an observed end (default "source-observed").
  topics         list|None Agenda topic strings; only used by detail levels 'topics'/'full' (not implemented yet).
  provenance     dict  Free-form passthrough for the caller; carried, never emitted, never hashed.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

LINK_KEYS = ("portal", "agenda", "packet", "minutes", "video")
LINK_LABELS = (("portal", "Portal"), ("agenda", "Agenda"), ("packet", "Packet"),
               ("minutes", "Minutes"), ("video", "Video"))


@dataclass
class MeetingRecord:
    uid_namespace: str
    record_id: str
    body: str
    meeting_type: str
    date: str
    start_time: str
    timezone: str
    status: Optional[str]
    retrieved_at: str
    location: Optional[str] = None
    links: Dict[str, Optional[str]] = field(default_factory=dict)
    documents: List[Dict[str, Any]] = field(default_factory=list)
    end_time: Optional[str] = None
    end_basis: Optional[str] = None
    topics: Optional[List[str]] = None
    provenance: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d):
        known = set(cls.__dataclass_fields__)
        extra = set(d) - known
        if extra:
            raise ValueError("unknown record fields: %s" % ", ".join(sorted(extra)))
        return cls(**d)


def records_from_json(data):
    """Accept a list of record dicts or {"records": [...]}."""
    if isinstance(data, dict):
        data = data["records"]
    return [r if isinstance(r, MeetingRecord) else MeetingRecord.from_dict(r) for r in data]
