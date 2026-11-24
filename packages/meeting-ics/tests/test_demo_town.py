"""Golden-file tests for the fictional demo-town feeds (CSV -> build-set), v1 and v2.

If a golden mismatch is intended: python3 tests/regen_demo_town.py, then review `git diff`.
"""
import datetime
import difflib
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

from meeting_ics import check

from tests import demo_town

try:
    import icalendar
except ImportError:  # optional; the test is skipped without it
    icalendar = None

try:
    import zoneinfo
except ImportError:  # pragma: no cover
    zoneinfo = None

FEEDS = ("all", "town-council", "planning-commission")
COUNTS = {"all": 11, "town-council": 7, "planning-commission": 4}
DOM = "@api.1507.systems"
PKG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def uid(rid):
    return "demo-town-%s%s" % (rid, DOM)


def feed_key(version, name):
    return "%s/calendar/demo-town/%s.ics" % (version, name)


class DemoTown(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.built = demo_town.build_all()
        cls.expected = {}
        for version, _csv, _stamp in demo_town.VERSIONS:
            demo_town.read_tree(os.path.join(demo_town.EXPECTED, version), version, cls.expected)

    def event(self, version, rid, name="all"):
        return check.parse_ics(self.built[feed_key(version, name)]).event(uid(rid))

    def test_golden_files_byte_identical(self):
        self.assertEqual(sorted(self.built), sorted(self.expected))
        self.assertEqual(len(self.built), 10)
        for key in sorted(self.built):
            if self.built[key] != self.expected[key]:
                diff = difflib.unified_diff(self.expected[key].decode().splitlines(),
                                            self.built[key].decode().splitlines(), "expected", "built", lineterm="")
                self.fail("%s differs from the golden file; if intended: python3 tests/regen_demo_town.py, then "
                          "review git diff\n%s" % (key, "\n".join(list(diff)[:40])))

    def test_regen_check_exits_zero(self):
        p = subprocess.run([sys.executable, os.path.join(PKG, "tests", "regen_demo_town.py"), "--check"],
                           capture_output=True, text=True, cwd=PKG)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_feeds_pass_check_and_counts(self):
        for version in ("v1", "v2"):
            for name in FEEDS:
                errors, _notes = check.validate(self.built[feed_key(version, name)], COUNTS[name])
                self.assertEqual(errors, [], "%s %s" % (version, name))

    def test_headers(self):
        text = self.built[feed_key("v1", "town-council")].decode()
        for line in ("METHOD:PUBLISH", "X-WR-CALNAME:Demo Town: Town Council", "X-WR-TIMEZONE:America/Chicago",
                     "REFRESH-INTERVAL;VALUE=DURATION:PT1H", "X-PUBLISHED-TTL:PT1H"):
            self.assertIn("\r\n" + line + "\r\n", text)
        self.assertIn("X-WR-CALNAME:Demo Town: All meetings", self.built[feed_key("v1", "all")].decode())

    def test_index(self):
        idx = json.loads(self.built["v2/calendar/index.json"])
        self.assertEqual(idx["totals"], {"cancelled": 2, "events": 11, "feeds": 3})
        self.assertEqual([f["key"] for f in idx["feeds"]],
                         ["calendar/demo-town/all.ics", "calendar/demo-town/planning-commission.ics",
                          "calendar/demo-town/town-council.ics"])
        self.assertEqual(idx["generated_at"], "2026-11-20T15:00:00Z")

    def test_v1_contents(self):
        cancelled = self.event("v1", "tc-102")
        self.assertEqual((cancelled.get("STATUS"), cancelled.get("SEQUENCE")), ("CANCELLED", "0"))
        for rid in ("pc-201", "tc-101", "tc-103", "tc-104", "pc-202", "tc-105", "pc-203", "tc-106", "pc-204",
                    "tc-107"):
            e = self.event("v1", rid)
            self.assertEqual(e.get("SEQUENCE"), "0", rid)
            self.assertEqual(e.get("STATUS"), "CONFIRMED", rid)
            self.assertEqual(e.get("DTSTAMP"), "20261001T150000Z", rid)
            self.assertEqual(e.get("LAST-MODIFIED"), "20261001T150000Z", rid)

    def test_v2_changes(self):
        pc201 = self.event("v2", "pc-201")
        self.assertEqual((pc201.get("SEQUENCE"), pc201.get("STATUS")), ("1", "CONFIRMED"))
        self.assertEqual(pc201.get("X-PROVENAGE-LIFECYCLE"), "completed")
        self.assertIn("Minutes: ", pc201.get("DESCRIPTION"))
        self.assertIn("Video: ", pc201.get("DESCRIPTION"))
        pc202 = self.event("v2", "pc-202")
        self.assertEqual((pc202.get("SEQUENCE"), pc202.get("STATUS")), ("1", "CANCELLED"))
        pc203 = self.event("v2", "pc-203")
        self.assertEqual((pc203.get("SEQUENCE"), pc203.get("STATUS")), ("1", "TENTATIVE"))
        self.assertEqual(pc203.get("SUMMARY"), "Postponed: Planning Commission Regular Meeting")
        tc106 = self.event("v2", "tc-106")
        self.assertEqual(tc106.get("SEQUENCE"), "1")
        self.assertEqual(tc106.get("UID"), uid("tc-106"))
        self.assertEqual(tc106.get("X-PROVENAGE-LIFECYCLE"), "rescheduled")
        start = [p for p in tc106.all("DTSTART")][0]
        self.assertEqual((start.params.get("TZID"), start.value), ("America/Chicago", "20261201T183000"))
        self.assertEqual(self.event("v2", "tc-102").get("SEQUENCE"), "0")
        self.assertEqual(self.event("v2", "tc-102").get("STATUS"), "CANCELLED")
        for rid in ("tc-101", "tc-103", "tc-104", "pc-204", "tc-105", "tc-107"):
            e = self.event("v2", rid)
            self.assertEqual(e.get("SEQUENCE"), "0", rid)
            self.assertEqual(e.get("LAST-MODIFIED"), "20261001T150000Z", rid)
        for e in check.parse_ics(self.built[feed_key("v2", "all")]).events:
            self.assertEqual(e.get("DTSTAMP"), "20261120T150000Z")

    def test_sequence_v1_to_v2_exactly_the_four_changed(self):
        old = json.loads(self.built["v1/ledger.json"])
        new = json.loads(self.built["v2/ledger.json"])
        self.assertEqual(sorted(old), sorted(new))
        self.assertTrue(all(v["sequence"] == 0 for v in old.values()))
        bumped = sorted(u for u in new if new[u]["sequence"] != old[u]["sequence"])
        self.assertEqual(bumped, sorted(uid(r) for r in ("pc-201", "pc-202", "pc-203", "tc-106")))
        self.assertTrue(all(new[u]["sequence"] == 1 for u in bumped))
        # per event in every v2 feed: SEQUENCE equals the ledger, so body feeds and all.ics agree
        for name in FEEDS:
            for e in check.parse_ics(self.built[feed_key("v2", name)]).events:
                self.assertEqual(int(e.get("SEQUENCE")), new[e.get("UID")]["sequence"], (name, e.get("UID")))

    def test_second_run_is_identical_and_bumps_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = os.path.join(tmp, "ledger.json")
            runs = []
            for run in ("a", "b"):
                out = os.path.join(tmp, run)
                code, stdout, err = demo_town.build_set("meetings-v2.csv", "2026-11-20T15:00:00Z", ledger, out)
                self.assertEqual(code, 0, err)
                files = {}
                demo_town.read_tree(out, "x", files)
                with open(ledger, "rb") as f:
                    files["ledger"] = f.read()
                runs.append((files, stdout))
            # run a starts from nothing, so it differs from golden v2 by design; run b must equal run a exactly
            self.assertEqual(runs[0][0], runs[1][0])
            self.assertIn("sequence_bumped=none", runs[1][1])
        # and the same through the stored v1 ledger: rebuilding v2 from golden v1 gives golden v2
        with tempfile.TemporaryDirectory() as tmp:
            ledger = os.path.join(tmp, "ledger.json")
            with open(ledger, "wb") as f:
                f.write(self.expected["v1/ledger.json"])
            code, stdout, err = demo_town.build_set("meetings-v2.csv", "2026-11-20T15:00:00Z", ledger,
                                                    os.path.join(tmp, "out"))
            self.assertEqual(code, 0, err)
            self.assertEqual(stdout.strip(), "feeds=3 events=11 cancelled=2 sequence_bumped=%s" % ",".join(
                sorted(uid(r) for r in ("pc-201", "pc-202", "pc-203", "tc-106"))))
            with open(ledger, "rb") as f:
                self.assertEqual(f.read(), self.expected["v2/ledger.json"])
            with open(os.path.join(tmp, "out", "calendar", "demo-town", "all.ics"), "rb") as f:
                self.assertEqual(f.read(), self.expected[feed_key("v2", "all")])

    def test_ledgers_cover_every_uid(self):
        ledger = json.loads(self.built["v1/ledger.json"])
        self.assertEqual(len(ledger), 11)
        self.assertTrue(all(k.startswith("demo-town-") and k.endswith(DOM) for k in ledger))

    @unittest.skipIf(zoneinfo is None, "zoneinfo is not available")
    def test_dst_offsets(self):
        try:
            tz = zoneinfo.ZoneInfo("America/Chicago")
        except zoneinfo.ZoneInfoNotFoundError:
            self.skipTest("tz data is not installed")
        # tc-103 is on CDT (23:30Z), tc-105 on CST (00:30Z on the next day): the same wall clock either side of 11-01
        for rid, start, utc in (("tc-103", "20261027T183000", "2026-10-27T23:30:00"),
                                ("tc-105", "20261110T183000", "2026-11-11T00:30:00")):
            p = self.event("v1", rid).all("DTSTART")[0]
            self.assertEqual((p.params["TZID"], p.value), ("America/Chicago", start))
            local = datetime.datetime.strptime(p.value, "%Y%m%dT%H%M%S").replace(tzinfo=tz)
            got = local.astimezone(datetime.timezone.utc).replace(tzinfo=None)
            self.assertEqual(got.isoformat(), utc, rid)
        tc105 = self.event("v1", "tc-105")
        self.assertEqual(tc105.all("DTEND")[0].value, "20261110T201500")
        self.assertEqual(tc105.get("X-PROVENAGE-DTEND-BASIS"), "source-observed")
        tc104 = self.event("v1", "tc-104")
        self.assertEqual(tc104.all("DTEND")[0].value, "20261103T183000")
        self.assertEqual(tc104.get("X-PROVENAGE-DTEND-BASIS"), "rule-study-session-90m")

    def test_vtimezone_is_the_builtin_us_rule_block(self):
        text = self.built[feed_key("v1", "all")].decode()
        self.assertIn("RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU", text)
        self.assertEqual(text.count("BEGIN:VTIMEZONE"), 1)

    def test_pc204_has_no_location_or_portal(self):
        e = self.event("v1", "pc-204")
        self.assertEqual([p.value for p in e.all("X-PROVENAGE-AMBIGUITY")],
                         ["no-portal-meeting-page-url", "no-location"])
        self.assertIsNone(e.get("URL"))
        self.assertIsNone(e.get("LOCATION"))

    def test_location_escaping(self):
        raw = self.built[feed_key("v1", "all")].decode().replace("\r\n ", "")
        self.assertIn("LOCATION:Demo Town Hall\\, Room 2B\\; 100 Main Street\\, Demo Town\r\n", raw)

    def test_tc106_remote_access(self):
        d = check.unescape_text(self.event("v1", "tc-106").get("DESCRIPTION"))
        self.assertIn("https://video.example.com/j/106", d)
        self.assertIn("+1 555 0100, ID 106", d)

    def test_everything_is_fictional(self):
        """Every golden output and every input fixture uses only reserved example hosts, the fixed UID domain and
        555-01xx phone numbers. A real host, e-mail address or phone number fails the test."""
        blobs = [b.decode().replace("\r\n ", "") for b in self.built.values()]
        for name in sorted(os.listdir(demo_town.FIXTURES)):
            path = os.path.join(demo_town.FIXTURES, name)
            if os.path.isfile(path):
                with open(path, encoding="utf-8") as f:
                    blobs.append(f.read())
        blob = "\n".join(blobs)
        self.assertNotIn("longmont", blob.lower())
        hosts = set(re.findall(r"https?://([^/\s\"\\,;]+)", blob))
        self.assertTrue(hosts)
        for host in hosts:
            self.assertRegex(host, r"^([a-z0-9-]+\.)*example(\.(com|org|net))?$", host)
        for addr in re.findall(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+)", blob):
            self.assertEqual(addr, DOM[1:])
        self.assertNotIn("https://www.", blob)
        no_hashes = re.sub(r"[0-9a-f]{16,}", "", blob)  # sha256 values contain long digit runs
        for phone in re.findall(r"\+\d[\d ()-]{6,}\d|\(?\d{3}\)?[ -]\d{3}[ -]\d{4}", no_hashes):
            digits = re.sub(r"\D", "", phone)
            self.assertRegex(digits, r"^1?55501\d\d$", phone)

    @unittest.skipIf(icalendar is None, "icalendar is not installed")
    def test_every_golden_ics_parses_with_icalendar(self):
        names = [k for k in self.expected if k.endswith(".ics")]
        self.assertEqual(len(names), 6)
        for key in names:
            with self.subTest(key):
                cal = icalendar.Calendar.from_ical(self.expected[key])
                events = [c for c in cal.walk() if c.name == "VEVENT"]
                self.assertEqual(len(events), COUNTS[os.path.basename(key)[:-4]])
                self.assertEqual(str(cal["VERSION"]), "2.0")

if __name__ == "__main__":
    unittest.main()
