"""Second, independent parse of built feeds with the test-only `icalendar` library (skipped if not installed)."""
import datetime
import unittest

from meeting_ics import BuildOptions, build
from meeting_ics.check import parse_ics, unescape_text

from tests.helpers import OPTS, rec

try:
    import icalendar
except ImportError:  # pragma: no cover
    icalendar = None

try:
    import zoneinfo
except ImportError:  # pragma: no cover
    zoneinfo = None


@unittest.skipIf(icalendar is None, "icalendar is not installed (test-only dependency)")
class RoundTrip(unittest.TestCase):
    def check(self, data):
        cal = icalendar.Calendar.from_ical(data)
        mine = parse_ics(data)
        events = [c for c in cal.walk("VEVENT")]
        self.assertEqual(len(events), len(mine.events))
        for ev in events:
            m = mine.event(str(ev["UID"]))
            self.assertEqual(int(ev["SEQUENCE"]), int(m.get("SEQUENCE")))
            self.assertEqual(str(ev["STATUS"]), m.get("STATUS"))
            self.assertEqual(str(ev["SUMMARY"]), unescape_text(m.get("SUMMARY")))
            if m.get("LOCATION") is not None:
                self.assertEqual(str(ev["LOCATION"]), unescape_text(m.get("LOCATION")))
            start = ev["DTSTART"].dt
            self.assertIsNotNone(start.tzinfo)
            if zoneinfo is not None:
                tzid = m.all("DTSTART")[0].params["TZID"]
                naive = start.replace(tzinfo=None)
                self.assertEqual(start.utcoffset(), naive.replace(tzinfo=zoneinfo.ZoneInfo(tzid)).utcoffset(),
                                 (m.get("UID"), start))
        return cal

    def test_default_build(self):
        recs = [rec(), rec(record_id="2", status="cancelled", location="Room 1, Hall; North"),
                rec(record_id="3", date="2026-07-04", status="postponed")]
        cal = self.check(build(recs, OPTS))
        self.assertEqual(str(cal["METHOD"]), "PUBLISH")

    def test_zones_across_dst(self):
        recs = [rec(record_id=str(i), timezone=tz, date=d)
                for i, (tz, d) in enumerate([("Europe/Berlin", "2026-03-29"), ("Europe/Berlin", "2026-10-25"),
                                             ("America/Chicago", "2026-11-01"), ("America/Chicago", "2026-03-08"),
                                             ("America/Phoenix", "2026-07-01"), ("UTC", "2026-07-01"),
                                             ("America/Denver", "2026-11-10"), ("Pacific/Auckland", "2026-09-27")])]
        self.check(build(recs, OPTS))

    def test_hostile_text(self):
        o = BuildOptions(uid_domain="example.org", calendar_name="a,b;c\\d", calendar_description="x\ny")
        self.check(build([rec(meeting_type="é" * 100 + ",;", location="中" * 60)], o))
        self.assertIsInstance(datetime.date.today(), datetime.date)


if __name__ == "__main__":
    unittest.main()
