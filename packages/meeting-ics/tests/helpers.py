"""Shared builders for the compatibility tests."""
from meeting_ics import BuildOptions, MeetingRecord

OPTS = BuildOptions(uid_domain="example.org")


def rec(**kw):
    d = dict(uid_namespace="t", record_id="1", body="Board", meeting_type="Board Meeting", date="2026-03-01",
             start_time="18:00", timezone="America/Denver", status="confirmed", retrieved_at="2026-01-01T00:00:00Z",
             links={"portal": "https://x/1"}, documents=[])
    d.update(kw)
    return MeetingRecord(**d)
