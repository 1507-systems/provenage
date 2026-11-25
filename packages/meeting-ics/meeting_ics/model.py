"""Source-agnostic input record for the builder.

A MeetingRecord is plain data; an adapter produces them from any upstream. Fields:

  uid_namespace  str   Prefix naming the source + jurisdiction, e.g. "examplegov-springfield". Together with
                       record_id and the uid_domain option it forms the stable UID
                       "<uid_namespace>-<record_id>@<uid_domain>".
  record_id      str   Identifier unique within the namespace (stable across re-fetches). An integer >= 0 in JSON
                       input is normalized to its string form.
  body           str   Governing body name, e.g. "City Council". In JSON input the key is `body_name`; `body` is
                       accepted as a legacy alias (give one, not both). `body_name` is a read-only property here.
  meeting_type   str   Type/title text, e.g. "City Council Regular Session". Used for SUMMARY and end rules.
  date           str   Local date "YYYY-MM-DD".
  start_time     str   Local start "HH:MM" (24-hour).
  timezone       str   IANA zone name, e.g. "America/Denver".
  status         str   One of the values in STATUS_EMIT (see the table below). Anything else is a validation error.
  location       str|None  Free text, may contain newlines.
  links          dict  Any of portal, agenda, packet, minutes, video (http(s) URL or None). Missing/None are omitted.
  documents      list  [{"kind": str, "url": str|None, "publish_date": str|None}] (flat strings). Only feeds the
                       change hash (revision detection), never emitted.
  retrieved_at   str   UTC fetch time "YYYY-MM-DDTHH:MM:SSZ"; becomes DTSTAMP (never the wall clock).
  end_time       str|None  Optional observed local end "HH:MM". Beats end rules and the default. An end earlier
                       than the start means "after midnight" and is accepted only if the meeting lasts <= 12 hours.
  end_basis      str|None  Label for an observed end (default "source-observed"). Only together with end_time.
  topics         list|None Agenda topic strings; only used by detail levels 'topics'/'full' (not implemented yet).
  provenance     dict  Free-form passthrough for the caller; carried, never emitted, never hashed.
  jurisdiction_slug, body_slug  str|None  Lowercase-hyphen slugs that route the record to a published feed.
                       Never emitted and never hashed; a body_slug needs a jurisdiction_slug.
  remote_access  dict|None  {"join_url": url|None, "dial_in": str|None}; None when both are empty. Emitted in
                       DESCRIPTION and part of the change hash (only when set).

Status mapping (input status -> STATUS, extra line, SUMMARY); <X> is the x_prefix option:

  scheduled                 CONFIRMED   (none)
  confirmed                 CONFIRMED   (alias of scheduled)
  scheduled_not_cancelled   CONFIRMED   <X>-STATUS-BASIS:derived-from-title-not-observed
  rescheduled               CONFIRMED   <X>-LIFECYCLE:rescheduled
  completed                 CONFIRMED   <X>-LIFECYCLE:completed
  postponed                 TENTATIVE   <X>-LIFECYCLE:postponed, SUMMARY prefixed "Postponed: "
  cancelled, canceled       CANCELLED   (the event stays in the feed), SUMMARY prefixed "Cancelled: "

Validation lives in meeting_ics.validate (the only record validator). Constructing a MeetingRecord directly
does no validation; build() validates every record.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

LINK_KEYS = ("portal", "agenda", "packet", "minutes", "video")
REMOTE_KEYS = ("join_url", "dial_in")
# input status -> (iCal STATUS, STATUS-BASIS value or None, LIFECYCLE value or None)
STATUS_EMIT = {
    "scheduled": ("CONFIRMED", None, None),
    "confirmed": ("CONFIRMED", None, None),
    "scheduled_not_cancelled": ("CONFIRMED", "derived-from-title-not-observed", None),
    "rescheduled": ("CONFIRMED", None, "rescheduled"),
    "completed": ("CONFIRMED", None, "completed"),
    "postponed": ("TENTATIVE", None, "postponed"),
    "cancelled": ("CANCELLED", None, None),
    "canceled": ("CANCELLED", None, None),
}
STATUS_CANONICAL = ("scheduled", "rescheduled", "cancelled", "postponed", "completed")
STATUS_ALIASES = {"confirmed": "scheduled", "scheduled_not_cancelled": "scheduled", "canceled": "cancelled"}
LINK_LABELS = (("portal", "Meeting page"), ("agenda", "Agenda"), ("packet", "Packet"),
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
    jurisdiction_slug: Optional[str] = None
    body_slug: Optional[str] = None
    remote_access: Optional[Dict[str, Optional[str]]] = None

    @property
    def body_name(self):
        return self.body

    @classmethod
    def from_dict(cls, d):
        """Validate one record dict (JSON field names) and build the record. Raises validate.RecordError."""
        from .validate import validate_record
        return cls(**validate_record(d, index=None))


def records_from_json(data):
    """Accept a list of record dicts or {"records": [...], "schema_version": 1}; validates every record."""
    from .validate import parse_input
    return parse_input(data)
