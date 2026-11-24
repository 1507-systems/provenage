"""Golden fixture captured from the builder at commit 86866a9 (before the M1 changes).

legacy_records.json is rebuilt with default options (plus uid_domain) and a fresh Ledger; the bytes must match
legacy_feed.ics exactly and the ledger entries must equal legacy_ledger.json. Later changes must keep this passing
(built with method=None and refresh_interval=None, which turn the new header defaults off) so existing
consumers' feeds do not drift.
"""
import json
import os
import unittest

from meeting_ics import BuildOptions, Ledger, build
from meeting_ics.model import records_from_json

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def _path(name):
    return os.path.join(FIXTURES, name)


class LegacyFixture(unittest.TestCase):
    def test_rebuild_matches_legacy_fixture(self):
        with open(_path("legacy_records.json"), encoding="utf-8") as f:
            records = records_from_json(json.load(f))
        ledger = Ledger()
        data = build(records, BuildOptions(uid_domain="example.org", method=None, refresh_interval=None), ledger)
        with open(_path("legacy_feed.ics"), "rb") as f:
            self.assertEqual(data, f.read())
        with open(_path("legacy_ledger.json"), encoding="utf-8") as f:
            self.assertEqual(ledger.entries, json.load(f))


if __name__ == "__main__":
    unittest.main()
