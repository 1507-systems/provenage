import datetime
import unittest

from meeting_ics import BuildOptions, Ledger, MeetingRecord, build
from meeting_ics.adapters import primegov_normalized
from meeting_ics.ical import esc, fold, us_dst_dates, vtimezone, zoneinfo_vtimezone


def rec(**kw):
    d = dict(uid_namespace="t", record_id="1", body="Board", meeting_type="Board Meeting", date="2026-03-01",
             start_time="18:00", timezone="America/Denver", status="confirmed", retrieved_at="2026-01-01T00:00:00Z",
             links={"portal": "https://x/1"}, documents=[])
    d.update(kw)
    return MeetingRecord(**d)


DOMAIN = "example.org"
OPTS = BuildOptions(uid_domain=DOMAIN)


class EscapeFold(unittest.TestCase):
    def test_escape(self):
        bs = chr(92)
        expected = "a" + bs + ",b" + bs + ";c" + bs * 2 + "d" + bs + "n" + "e" + bs + "nf"
        self.assertEqual(esc("a,b;c\\d\ne\r\nf"), expected)

    def test_fold_short(self):
        self.assertEqual(fold("x" * 75), ["x" * 75])

    def test_fold_long_octets(self):
        parts = fold("A:" + "x" * 200)
        self.assertTrue(all(len(p.encode()) <= 75 for p in parts))
        self.assertTrue(all(p.startswith(" ") for p in parts[1:]))
        self.assertEqual("".join(parts[:1] + [p[1:] for p in parts[1:]]), "A:" + "x" * 200)

    def test_fold_never_splits_utf8(self):
        for pad in range(0, 4):
            line = "S:" + "a" * pad + "é中\U0001F600" * 40
            parts = fold(line)
            for p in parts:
                self.assertLessEqual(len(p.encode()), 75)
                p.encode("utf-8").decode("utf-8")
            self.assertEqual(parts[0] + "".join(p[1:] for p in parts[1:]), line)


class Timezone(unittest.TestCase):
    def test_denver_dst_dates(self):
        want = {2024: ((3, 10), (11, 3)), 2025: ((3, 9), (11, 2)), 2026: ((3, 8), (11, 1))}
        for y, ((sm, sd), (em, ed)) in want.items():
            s, e = us_dst_dates(y)
            self.assertEqual((s.month, s.day, e.month, e.day), (sm, sd, em, ed))

    def test_denver_block_exact(self):
        tz = vtimezone("America/Denver", [2026])
        self.assertIn("RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU", tz)
        self.assertIn("TZOFFSETTO:-0600", tz)
        self.assertEqual(tz[0:3], ["BEGIN:VTIMEZONE", "TZID:America/Denver", "BEGIN:STANDARD"])

    def test_zoneinfo_derived_matches_dates(self):
        tz = zoneinfo_vtimezone("America/Denver", [2024, 2025, 2026])
        starts = [x for x in tz if x.startswith("DTSTART:")]
        for d in ("20240310T020000", "20241103T020000", "20250309T020000", "20251102T020000",
                  "20260308T020000", "20261101T020000"):
            self.assertIn("DTSTART:" + d, starts)

    def test_other_zone(self):
        tz = vtimezone("Europe/Berlin", [2026])
        self.assertIn("TZID:Europe/Berlin", tz)
        self.assertTrue(any("DTSTART:20260329T020000" == x for x in tz))

    def test_unknown_zone_fails_loudly(self):
        with self.assertRaises(RuntimeError):
            vtimezone("Not/AZone", [2026])


class LedgerSemantics(unittest.TestCase):
    def test_first_sight_then_stable_then_bump(self):
        led = Ledger()
        build([rec()], OPTS, led)
        e = led.entries["t-1@" + DOMAIN]
        self.assertEqual((e["sequence"], e["last_modified"]), (0, "2026-01-01T00:00:00Z"))
        build([rec()], OPTS, led)
        self.assertEqual(led.entries["t-1@" + DOMAIN]["sequence"], 0)
        build([rec(start_time="18:30", retrieved_at="2026-02-01T00:00:00Z")], OPTS, led)
        e = led.entries["t-1@" + DOMAIN]
        self.assertEqual((e["sequence"], e["last_modified"]), (1, "2026-02-01T00:00:00Z"))

    def test_docs_change_bumps(self):
        led = Ledger()
        build([rec()], OPTS, led)
        build([rec(links={"portal": "https://x/1", "video": "https://v"})], OPTS, led)
        self.assertEqual(led.entries["t-1@" + DOMAIN]["sequence"], 1)

    def test_absent_uid_kept(self):
        led = Ledger()
        build([rec(), rec(record_id="2")], OPTS, led)
        build([rec()], OPTS, led)
        self.assertIn("t-2@" + DOMAIN, led.entries)

    def test_end_rule_change_bumps(self):
        led = Ledger()
        build([rec()], OPTS, led)
        o2 = BuildOptions(uid_domain="example.org", default_duration=datetime.timedelta(hours=2))
        build([rec()], o2, led)
        self.assertEqual(led.entries["t-1@" + DOMAIN]["sequence"], 1)


class Behavior(unittest.TestCase):
    def test_determinism(self):
        recs = [rec(), rec(record_id="2", status="cancelled")]
        self.assertEqual(build(recs, OPTS, Ledger()), build(recs, OPTS, Ledger()))

    def test_end_rules_and_prefix(self):
        o = BuildOptions(uid_domain="d", x_prefix="X-ACME", end_rules=[("board", "21:00")])
        out = build([rec()], o).decode()
        self.assertIn("DTEND;TZID=America/Denver:20260301T210000", out)
        self.assertIn("X-ACME-DTEND-BASIS:rule-end-2100-local", out)

    def test_stub_detail_levels(self):
        for lvl in ("topics", "full"):
            with self.assertRaises(NotImplementedError):
                build([rec()], BuildOptions(detail=lvl))

    def test_no_rsvp_properties(self):
        out = build([rec()], OPTS).decode()
        for bad in ("ATTENDEE", "RSVP", "ORGANIZER"):
            self.assertNotIn(bad, out)


FIXTURE = {"meetings": [{
    "meeting_id": {"value": 42, "provenance": {"retrieved_at": "2026-01-01T00:00:00Z"}},
    "body": {"value": "City Council"}, "meeting_type": {"value": "Regular Session"},
    "date": {"value": "2026-03-02"}, "start_time": {"value": "18:00"}, "timezone": {"value": "America/Denver"},
    "status": {"value": "scheduled_not_cancelled"}, "location": {"value": None},
    "meeting_url": {"value": "https://example.org/m/42"}, "agenda_url": {"value": None},
    "packet_url": {"value": None}, "minutes_url": {"value": None}, "video_url": {"value": None},
    "jurisdiction": {"value": "example-co"}, "documents": []}]}


class PrimeGovAdapter(unittest.TestCase):
    def test_uid_namespace_is_required(self):
        with self.assertRaises(TypeError):
            primegov_normalized.convert(FIXTURE)
        with self.assertRaises(TypeError):
            primegov_normalized.load("unused.json")

    def test_convert_and_build(self):
        recs = primegov_normalized.convert(FIXTURE, uid_namespace="primegov-example")
        self.assertEqual(recs[0].uid_namespace, "primegov-example")
        self.assertEqual(recs[0].record_id, "42")
        out = build(recs, OPTS).decode()
        self.assertIn("UID:primegov-example-42@" + DOMAIN, out)


class DuplicateUid(unittest.TestCase):
    def test_duplicate_uid_raises(self):
        with self.assertRaises(ValueError):
            build([rec(), rec()], OPTS)

    def test_distinct_uids_ok(self):
        build([rec(), rec(record_id="2")], OPTS)


if __name__ == "__main__":
    unittest.main()
