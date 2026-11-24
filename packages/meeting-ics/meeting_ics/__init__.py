"""meeting_ics: generic civic meeting records -> RFC 5545 iCalendar feed.

Standard library only. Jurisdiction- and source-agnostic: source-specific knowledge lives in adapters/.
Calendar building only: no RSVP, attendance or member data is ever part of this library or the public feed.
"""
from .build import BuildOptions, build, build_with_report
from .feedset import FeedFile, FeedSet, build_feedset
from .ledger import Ledger
from .model import STATUS_ALIASES, STATUS_CANONICAL, STATUS_EMIT, MeetingRecord, records_from_json
from .validate import SLUG_RE, RecordError, load_input, parse_input, validate_records

__all__ = ["MeetingRecord", "records_from_json", "Ledger", "BuildOptions", "build", "build_with_report",
           "FeedSet", "FeedFile", "build_feedset", "RecordError", "parse_input", "validate_records", "load_input",
           "STATUS_EMIT", "STATUS_CANONICAL", "STATUS_ALIASES", "SLUG_RE"]
__version__ = "0.2.0"
