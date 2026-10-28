"""meeting_ics: generic civic meeting records -> RFC 5545 iCalendar feed.

Standard library only. Jurisdiction- and source-agnostic: source-specific knowledge lives in adapters/.
Calendar building only: no RSVP, attendance or member data is ever part of this library or the public feed.
"""
from .build import BuildOptions, build, build_with_report
from .ledger import Ledger
from .model import MeetingRecord, records_from_json

__all__ = ["MeetingRecord", "records_from_json", "Ledger", "BuildOptions", "build", "build_with_report"]
__version__ = "0.1.0"
