"""0.2.1: Cancelled: prefix, Meeting page label, default_duration option."""
import datetime
import json
import os
import unittest

from meeting_ics import BuildOptions, Ledger, build, build_with_report
from meeting_ics.model import records_from_json

from tests.test_c1_build import good

OPTS = BuildOptions(uid_domain="example.org")
FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def summary(**kw):
    out = build(records_from_json([good(**kw)]), OPTS, Ledger()).decode()
    return [ln for ln in out.split("\r\n") if ln.startswith("SUMMARY:")][0]


class CancelledPrefix(unittest.TestCase):
    def test_prefix_added_once(self):
        self.assertEqual(summary(status="cancelled"), "SUMMARY:Cancelled: City Council Regular Session")

    def test_idempotent_for_source_prefixes(self):
        for title in ("Cancelled: Regular Session", "CANCELED: Regular Session", "canceled:Regular Session",
                      "Cancelled: City Council Regular Session"):
            with self.subTest(title=title):
                self.assertEqual(summary(status="cancelled", meeting_type=title),
                                 "SUMMARY:Cancelled: City Council Regular Session")

    def test_status_cancelled_kept_and_other_statuses_unprefixed(self):
        out = build(records_from_json([good(status="cancelled")]), OPTS, Ledger()).decode()
        self.assertIn("\r\nSTATUS:CANCELLED\r\n", out)
        self.assertEqual(summary(status="scheduled"), "SUMMARY:City Council Regular Session")
        self.assertEqual(summary(status="scheduled", meeting_type="Cancelled: Regular Session"),
                         "SUMMARY:City Council Cancelled: Regular Session")

    def test_prefix_does_not_bump_sequence(self):
        # The prefix is derived output, not part of the change hash: a ledger written by 0.2.0 stays valid.
        with open(os.path.join(FIXTURES, "legacy_records.json"), encoding="utf-8") as f:
            records = records_from_json(json.load(f))
        with open(os.path.join(FIXTURES, "legacy_ledger.json"), encoding="utf-8") as f:
            entries = json.load(f)
        ledger = Ledger()
        ledger.entries = json.loads(json.dumps(entries))
        _, rep = build_with_report(records, BuildOptions(uid_domain="example.org", method=None,
                                                         refresh_interval=None), ledger)
        self.assertEqual(rep["bumped"], [])
        self.assertEqual(ledger.entries, entries)


class MeetingPageLabel(unittest.TestCase):
    def test_label(self):
        out = build(records_from_json([good()]), OPTS, Ledger()).decode()
        self.assertIn("DESCRIPTION:Meeting page: https://demo.example/m/1\r\n", out)
        self.assertNotIn("Portal:", out)


class DefaultDuration(unittest.TestCase):
    def end(self, cfg):
        opts = BuildOptions.from_dict(dict({"uid_domain": "example.org"}, **cfg))
        out = build(records_from_json([good()]), opts, Ledger()).decode()
        return [ln for ln in out.split("\r\n") if ln.startswith("DTEND")][0], out

    def test_default_unchanged(self):
        self.assertEqual(BuildOptions().default_duration, datetime.timedelta(hours=3))
        self.assertEqual(self.end({})[0], "DTEND;TZID=America/Denver:20261201T210000")

    def test_iso_duration(self):
        line, out = self.end({"default_duration": "PT90M"})
        self.assertEqual(line, "DTEND;TZID=America/Denver:20261201T193000")
        self.assertIn("DTEND-BASIS:default-duration-90m-assumed", out)
        self.assertEqual(BuildOptions.from_dict({"default_duration": "PT2H"}).default_duration,
                         datetime.timedelta(hours=2))

    def test_bad_values(self):
        for cfg in ({"default_duration": "90"}, {"default_duration": "PT0S"}, {"default_duration": "P"},
                    {"default_duration": "PT30S"}, {"default_duration": "PT1H", "default_duration_hours": 2}):
            with self.subTest(cfg=cfg), self.assertRaises(ValueError):
                BuildOptions.from_dict(cfg).validate()


if __name__ == "__main__":
    unittest.main()
