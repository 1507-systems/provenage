"""Build and CLI behaviour added with the input contract: status mapping, remote_access, validation in build()."""
import contextlib
import io
import json
import os
import tempfile
import unittest

from meeting_ics import BuildOptions, Ledger, MeetingRecord, RecordError, build, build_with_report, parse_input
from meeting_ics.cli import main

OPTS = BuildOptions(uid_domain="example.org")
FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
RA = {"join_url": "https://zoom.example/j/1", "dial_in": "+1 555 0100\nID 123"}


def good(**kw):
    d = {"uid_namespace": "demo", "record_id": "m1", "jurisdiction_slug": "demo-town", "body_slug": "city-council",
         "body_name": "City Council", "meeting_type": "Regular Session", "date": "2026-12-01", "start_time": "18:00",
         "timezone": "America/Denver", "status": "scheduled", "retrieved_at": "2026-11-24T12:00:00Z",
         "links": {"portal": "https://demo.example/m/1"}, "location": "Town Hall"}
    d.update(kw)
    return d


def ics(rec_dict, ledger=None):
    return build(parse_input([rec_dict]), OPTS, ledger or Ledger()).decode()


def unfolded(text):
    return text.replace("\r\n ", "")


class StatusTable(unittest.TestCase):
    # status -> (STATUS line, extra lines present, lines absent)
    CASES = {
        "scheduled": ("CONFIRMED", [], ["LIFECYCLE", "STATUS-BASIS"]),
        "confirmed": ("CONFIRMED", [], ["LIFECYCLE", "STATUS-BASIS"]),
        "scheduled_not_cancelled": ("CONFIRMED", ["X-MEETING-ICS-STATUS-BASIS:derived-from-title-not-observed"],
                                    ["LIFECYCLE"]),
        "rescheduled": ("CONFIRMED", ["X-MEETING-ICS-LIFECYCLE:rescheduled"], ["STATUS-BASIS"]),
        "completed": ("CONFIRMED", ["X-MEETING-ICS-LIFECYCLE:completed"], ["STATUS-BASIS"]),
        "postponed": ("TENTATIVE", ["X-MEETING-ICS-LIFECYCLE:postponed",
                                    "SUMMARY:Postponed: City Council Regular Session"], ["STATUS-BASIS"]),
        "cancelled": ("CANCELLED", [], ["LIFECYCLE", "STATUS-BASIS"]),
        "canceled": ("CANCELLED", [], ["LIFECYCLE", "STATUS-BASIS"]),
    }

    def test_each_status(self):
        for status, (ical, present, absent) in self.CASES.items():
            with self.subTest(status=status):
                out = ics(good(status=status))
                self.assertEqual(out.count("STATUS:"), 1)
                self.assertIn("\r\nSTATUS:%s\r\n" % ical, out)
                for line in present:
                    self.assertIn("\r\n" + line + "\r\n", out)
                for word in absent:
                    self.assertNotIn(word, out)
                if status != "postponed":
                    self.assertIn("SUMMARY:City Council Regular Session", out)
                self.assertNotIn("unmapped-status", out)

    def test_cancelled_counted_and_kept(self):
        for status, n in (("cancelled", 1), ("canceled", 1), ("scheduled", 0)):
            _, rep = build_with_report(parse_input([good(status=status)]), OPTS)
            self.assertEqual((rep["events"], rep["cancelled"]), (1, n))

    def test_status_lines_after_status_in_order(self):
        out = ics(good(status="scheduled_not_cancelled"))
        self.assertLess(out.index("STATUS:CONFIRMED"), out.index("X-MEETING-ICS-STATUS-BASIS"))


class Sequence(unittest.TestCase):
    def seq(self, out):
        return int(out.split("SEQUENCE:")[1].split("\r\n")[0])

    def test_status_driven_bumps(self):
        led = Ledger()
        self.assertEqual(self.seq(ics(good(), led)), 0)
        self.assertEqual(self.seq(ics(good(), led)), 0)
        self.assertEqual(self.seq(ics(good(status="rescheduled", date="2026-12-08"), led)), 1)
        self.assertEqual(self.seq(ics(good(status="postponed", date="2026-12-08"), led)), 2)

    def test_slugs_not_hashed(self):
        led = Ledger()
        ics(good(jurisdiction_slug=None, body_slug=None), led)
        self.assertEqual(self.seq(ics(good(), led)), 0)

    def test_alias_is_hashed_raw(self):
        led = Ledger()
        ics(good(status="cancelled"), led)
        self.assertEqual(self.seq(ics(good(status="canceled"), led)), 1)


class RemoteAccess(unittest.TestCase):
    def test_description_and_bump(self):
        led = Ledger()
        ics(good(), led)
        out = ics(good(remote_access=RA), led)
        self.assertIn("DESCRIPTION:Portal: https://demo.example/m/1\\nJoin online: https://zoom.example/j/1"
                      "\\nDial-in: +1 555 0100\\nID 123", unfolded(out))
        self.assertIn("SEQUENCE:1", out)

    def test_partial_remote(self):
        out = unfolded(ics(good(remote_access={"dial_in": "+1 555 0100"})))
        self.assertIn("Dial-in: +1 555 0100", out)
        self.assertNotIn("Join online", out)


class BuildValidates(unittest.TestCase):
    def test_empty(self):
        with self.assertRaises(RecordError) as cm:
            build([], OPTS)
        self.assertEqual(cm.exception.path, "<input>")

    def test_bad_status_and_null(self):
        base = dict(uid_namespace="t", record_id="1", body="B", meeting_type="M", date="2026-03-01",
                    start_time="18:00", timezone="America/Denver", retrieved_at="2026-01-01T00:00:00Z")
        for status, msg in ((None, "null"), ("foo", "unknown status")):
            with self.assertRaises(RecordError) as cm:
                build([MeetingRecord(status=status, **base)], OPTS)
            self.assertEqual(cm.exception.path, "[0].status")
            self.assertIn(msg, cm.exception.message)

    def test_documents_with_none_sort(self):
        a = [{"kind": "agenda_pdf", "url": None}, {"kind": "agenda_pdf", "url": "https://x/a.pdf"}]
        l1, l2 = Ledger(), Ledger()
        o1 = build(parse_input([good(documents=a)]), OPTS, l1)
        o2 = build(parse_input([good(documents=a[::-1])]), OPTS, l2)
        self.assertEqual(o1, o2)
        self.assertEqual(l1.entries, l2.entries)

    def test_overnight_end(self):
        self.assertIn("DTEND;TZID=America/Denver:20261202T010000", ics(good(end_time="01:00")))

    def test_generic_fixture(self):
        with open(os.path.join(FIXTURES, "generic_valid.json"), encoding="utf-8") as f:
            recs = parse_input(json.load(f))
        out = build(recs, OPTS, Ledger())
        text = out.decode("utf-8")
        self.assertEqual(text.count("\r\n"), text.count("\n"))
        for line in text.split("\r\n"):
            self.assertLessEqual(len(line.encode("utf-8")), 75)
        uids = [ln for ln in text.split("\r\n") if ln.startswith("UID:")]
        self.assertEqual((len(uids), len(set(uids))), (3, 3))
        self.assertIn("STATUS:TENTATIVE", text)
        self.assertIn("Join online: https://video.example.com/j/103", unfolded(text))


class Cli(unittest.TestCase):
    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(args))
        return code, out.getvalue(), err.getvalue()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def path(self, name):
        return os.path.join(self.tmp.name, name)

    def write(self, name, obj):
        p = self.path(name)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(obj, f)
        return p

    def test_validate_ok(self):
        code, out, err = self.run_cli("validate", "--input", self.write("in.json", [good()]))
        self.assertEqual((code, out, err), (0, "ok records=1\n", ""))

    def test_validate_invalid(self):
        code, out, err = self.run_cli("validate", "--input", self.write("in.json", [good(date="x", status="tbd")]))
        self.assertEqual((code, out), (2, ""))
        lines = err.splitlines()
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[0].startswith("[0].date: "))
        self.assertIn("(record_id 'm1')", lines[0])
        self.assertEqual(lines[-1], "meeting_ics: 2 error(s); input is invalid")

    def test_build_invalid_writes_nothing(self):
        out_p, led_p = self.path("o/feed.ics"), self.path("o/ledger.json")
        code, _, err = self.run_cli("build", "--input", self.write("in.json", [good(date="x")]),
                                    "--output", out_p, "--ledger", led_p)
        self.assertEqual(code, 2)
        self.assertTrue(err.rstrip().endswith("1 error(s); nothing written"))
        self.assertFalse(os.path.exists(out_p) or os.path.exists(led_p) or os.path.exists(os.path.dirname(out_p)))

    def test_build_ok_output_line(self):
        out_p, led_p = self.path("feed.ics"), self.path("ledger.json")
        code, out, _ = self.run_cli("build", "--input", self.write("in.json", [good()]), "--output", out_p,
                                    "--ledger", led_p)
        self.assertEqual((code, out), (0, "events=1 cancelled=0 sequence_bumped=none\n"))
        self.assertTrue(os.path.exists(out_p) and os.path.exists(led_p))

    def test_no_portal_emits_ambiguity_line(self):
        self.assertIn("X-MEETING-ICS-AMBIGUITY:no-portal-meeting-page-url", ics(good(links={"portal": ""})))
        self.assertNotIn("no-portal-meeting-page-url", ics(good()))

    def test_bad_config_values_exit_2_not_traceback(self):
        inp = self.write("in.json", [good()])
        for i, cfg in enumerate(({"default_duration_hours": "x"}, {"default_duration_minutes": [1]},
                                 {"default_duration_hours": 0}, {"end_rules": ["x"]}, {"end_rules": [5]},
                                 {"end_rules": [["Board", {"duration_minutes": "x"}]]},
                                 {"end_rules": [["Board", {"duration_minutes": -5}]]}, {"prodid": "a,b"})):
            with self.subTest(cfg):
                code, out, err = self.run_cli("build", "--input", inp, "--output", self.path("o%d.ics" % i),
                                              "--ledger", self.path("l%d.json" % i),
                                              "--config", self.write("c%d.json" % i, cfg))
                self.assertEqual((code, out), (2, ""))
                self.assertTrue(err.startswith("meeting_ics: error: "), err)
                self.assertNotIn("Traceback", err)
                self.assertFalse(os.path.exists(self.path("o%d.ics" % i)))

    def test_other_errors(self):
        code, _, err = self.run_cli("validate", "--input", self.path("missing.json"))
        self.assertEqual(code, 2)
        self.assertIn("<input>: cannot read", err)
        code, _, err = self.run_cli("validate", "--input", self.path("x.json"), "--adapter", "primegov_normalized")
        self.assertEqual((code, err), (2, "meeting_ics: error: --uid-namespace is required with --adapter\n"))
        code, _, err = self.run_cli("validate", "--input", self.path("x.json"), "--adapter", "nope",
                                    "--uid-namespace", "n")
        self.assertEqual((code, err), (2, "meeting_ics: error: unknown adapter 'nope'\n"))
        bad_ledger = self.path("bad-ledger.json")
        with open(bad_ledger, "w", encoding="utf-8") as f:
            f.write("{not json")
        code, _, err = self.run_cli("build", "--input", self.write("in.json", [good()]), "--output",
                                    self.path("f.ics"), "--ledger", bad_ledger)
        self.assertEqual(code, 2)
        self.assertTrue(err.startswith("meeting_ics: error: "), err)
        self.assertFalse(os.path.exists(self.path("f.ics")))


if __name__ == "__main__":
    unittest.main()
