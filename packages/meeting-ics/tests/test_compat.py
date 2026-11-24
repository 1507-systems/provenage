"""C3: client-compatibility options, header order, text hardening, CLI check-feed and template."""
import contextlib
import io
import os
import tempfile
import unittest

from meeting_ics import BuildOptions, Ledger, build, build_with_report
from meeting_ics.check import parse_ics, unescape_text, validate
from meeting_ics.cli import main
from meeting_ics.ical import esc, fold

from tests.helpers import OPTS, rec

CRLF = "\r\n"


def lines(data):
    return data.decode("utf-8").split(CRLF)


class Headers(unittest.TestCase):
    def test_default_header_order(self):
        out = lines(build([rec()], OPTS))
        self.assertEqual(out[:8], ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//meeting_ics//meeting_ics 0.1//EN",
                                   "CALSCALE:GREGORIAN", "METHOD:PUBLISH", "X-WR-TIMEZONE:America/Denver",
                                   "REFRESH-INTERVAL;VALUE=DURATION:PT1H", "X-PUBLISHED-TTL:PT1H"])
        self.assertEqual(out[8], "BEGIN:VTIMEZONE")

    def test_full_header_order(self):
        o = BuildOptions(uid_domain="example.org", calendar_name="N", calendar_description="D")
        out = lines(build([rec()], o))
        self.assertEqual(out[4:10], ["METHOD:PUBLISH", "X-WR-CALNAME:N", "X-WR-CALDESC:D",
                                     "X-WR-TIMEZONE:America/Denver", "REFRESH-INTERVAL;VALUE=DURATION:PT1H",
                                     "X-PUBLISHED-TTL:PT1H"])

    def test_two_zones_have_no_x_wr_timezone(self):
        text = build([rec(), rec(record_id="2", timezone="America/Chicago")], OPTS).decode()
        self.assertNotIn("X-WR-TIMEZONE", text)

    def test_hints_off(self):
        text = build([rec()], BuildOptions(uid_domain="example.org", method=None, refresh_interval=None)).decode()
        for needle in ("METHOD", "REFRESH-INTERVAL", "X-PUBLISHED-TTL"):
            self.assertNotIn(needle, text)

    def test_hints_do_not_change_sequence(self):
        led = Ledger()
        build([rec()], OPTS, led)
        _, rep = build_with_report([rec()], BuildOptions(uid_domain="example.org", refresh_interval="P1D"), led)
        self.assertEqual(rep["bumped"], [])

    def test_description_round_trip(self):
        o = BuildOptions(uid_domain="example.org", calendar_description="a,b;c\nd")
        data = build([rec()], o)
        self.assertIn(b"X-WR-CALDESC:a\\,b\\;c\\nd\r\n", data)
        self.assertEqual(unescape_text(parse_ics(data).get("X-WR-CALDESC")), "a,b;c\nd")


class OptionsValidation(unittest.TestCase):
    def bad(self, **kw):
        with self.assertRaises(ValueError) as cm:
            build([rec()], BuildOptions(uid_domain="example.org", **kw))
        return str(cm.exception)

    def test_refresh_interval(self):
        for ok in ("P1D", "PT30M", "P1W", "PT1H", "P1DT2H"):
            build([rec()], BuildOptions(uid_domain="example.org", refresh_interval=ok))
        for bad in ("PT", "P", "1H", "PT0H", "P1DT", "pt1h", "PT1H\r\nATTENDEE:x", "PT-1H", 5):
            self.assertTrue(self.bad(refresh_interval=bad).startswith("refresh_interval must be"), bad)

    def test_other_fields(self):
        self.assertTrue(self.bad(method="REQUEST").startswith('method must be "PUBLISH" or null'))
        self.assertTrue(self.bad(prodid="a\r\nb").startswith("prodid must be non-empty"))
        self.assertTrue(self.bad(prodid="").startswith("prodid must be non-empty"))
        self.assertTrue(self.bad(x_prefix="Y-A").startswith("x_prefix must look like"))
        self.assertTrue(self.bad(x_prefix="X-A\r\nB").startswith("x_prefix must look like"))
        self.assertEqual(self.bad(calendar_name=5), "calendar_name must be text or null")
        self.assertEqual(self.bad(calendar_description=["x"]), "calendar_description must be text or null")
        with self.assertRaises(ValueError) as cm:
            build([rec()], BuildOptions(uid_domain="bad domain\r\n"))
        self.assertTrue(str(cm.exception).startswith("uid_domain must be a host name"))

    def test_end_rule_basis(self):
        o = BuildOptions.from_dict({"uid_domain": "example.org",
                                    "end_rules": [{"match": "x", "end_local": "20:00", "basis": "obs\r\nATTENDEE:x"}]})
        with self.assertRaises(ValueError) as cm:
            build([rec()], o)
        self.assertEqual(str(cm.exception), "end_rules[0].basis must match [A-Za-z0-9][A-Za-z0-9._-]* (max 64), "
                                            "got 'obs\\r\\nATTENDEE:x'")
        for basis in ("a" * 65, "-x", 5):
            o = BuildOptions.from_dict({"uid_domain": "example.org",
                                        "end_rules": [["y", "20:00"], {"match": "x", "end_local": "20:00",
                                                                       "basis": basis}]})
            with self.assertRaisesRegex(ValueError, r"end_rules\[1\]\.basis"):
                build([rec()], o)
        ok = BuildOptions.from_dict({"uid_domain": "example.org",
                                     "end_rules": [{"match": "board", "end_local": "20:00", "basis": "rule-x"}]})
        self.assertIn("-DTEND-BASIS:rule-x\r\n", build([rec()], ok).decode())

    def test_validation_runs_after_detail_check(self):
        with self.assertRaises(NotImplementedError):
            build([rec()], BuildOptions(detail="topics", prodid=""))

    def test_from_dict_unknown_keys(self):
        with self.assertRaises(ValueError) as cm:
            BuildOptions.from_dict({"zeta": 1, "bogus": 2})
        self.assertEqual(str(cm.exception), "unknown option(s): bogus, zeta")
        o = BuildOptions.from_dict({"refresh_interval": None, "method": None, "end_rules": None})
        self.assertIsNone(o.refresh_interval)
        self.assertIsNone(o.method)
        self.assertEqual(o.end_rules, [])


HOSTILE = {
    "H1": "a,b;c\\d", "H3": "t\tx", "H5": "x" * 1000, "H6": "é" * 200, "H7": "中" * 100, "H8": "😀" * 100,
    "H9": "👩‍👩‍👧" * 20, "H10": "\\" + "n", "H11": "end\\", "H12": ";;;,,,",
}


def norm(s):
    out = s.replace("\r\n", "\n").replace("\r", "\n")
    return "".join(c for c in out if not ("\x00" <= c <= "\x08" or "\x0b" <= c <= "\x1f" or c == "\x7f"))


class Hostile(unittest.TestCase):
    def test_round_trip(self):
        for name, s in HOSTILE.items():
            with self.subTest(name):
                o = BuildOptions(uid_domain="example.org", calendar_name=s, calendar_description=s)
                # Record fields reject tabs and over-long text (validate.py); those strings go through config only.
                in_record = name not in ("H3", "H5")
                data = build([rec(meeting_type=s, location=s)] if in_record else [rec()], o)
                errors, _ = validate(data)
                self.assertEqual(errors, [])
                self.assertTrue(all(len(p.encode()) <= 75 for p in lines(data)))
                cal = parse_ics(data)
                ev = cal.events[0]
                self.assertEqual(unescape_text(cal.get("X-WR-CALNAME")), norm(s))
                self.assertEqual(unescape_text(cal.get("X-WR-CALDESC")), norm(s))
                if in_record:
                    self.assertEqual(unescape_text(ev.get("SUMMARY")), "Board " + norm(s))
                    self.assertEqual(unescape_text(ev.get("LOCATION")), norm(s))

    def test_config_text_with_control_characters(self):
        for name, s in {"H2": "l1\nl2\r\nl3\rl4", "H4": "n\x00b\x07e\x1bd\x7f"}.items():
            with self.subTest(name):
                o = BuildOptions(uid_domain="example.org", calendar_name=s, calendar_description=s)
                data = build([rec()], o)
                self.assertEqual(validate(data)[0], [])
                cal = parse_ics(data)
                self.assertEqual(unescape_text(cal.get("X-WR-CALNAME")), norm(s))
                self.assertEqual(unescape_text(cal.get("X-WR-CALDESC")), norm(s))

    def test_backslash_n_is_not_a_newline(self):
        self.assertEqual(unescape_text(esc("\\" + "n")), "\\n")

    def test_esc_strips_controls_keeps_tab(self):
        self.assertEqual(esc("x\x00y"), "xy")
        self.assertEqual(esc("\t"), "\t")
        self.assertEqual(esc("a\x7fb\x1bc\x0bd"), "abcd")
        self.assertEqual(esc("a\r\nb\rc\nd"), "a\\nb\\nc\\nd")

    def test_control_char_in_record_field_is_rejected(self):
        with self.assertRaises(ValueError):
            build([rec(meeting_type="a\r\nATTENDEE:x")], OPTS)

    def test_exact_folds(self):
        self.assertEqual(len(fold("X:" + "a" * 71 + "é")), 1)
        self.assertEqual(fold("X:" + "a" * 72 + "é"), ["X:" + "a" * 72, " é"])
        self.assertEqual(fold("X:" + "a" * 72 + "中"), ["X:" + "a" * 72, " 中"])
        self.assertEqual(fold("X:" + "a" * 70 + "😀"), ["X:" + "a" * 70, " 😀"])
        self.assertEqual(len(fold("X:" + "a" * 69 + "😀")), 1)
        parts = fold("X:" + "a" * 73 + "b" * 74 + "c")
        self.assertEqual([len(p) for p in parts], [75, 75, 2])
        self.assertEqual(parts[-1], " c")

    def test_dtend_basis_is_escaped(self):
        data = build([rec(end_time="20:00", end_basis="obs-x")], OPTS)
        self.assertIn(b"-DTEND-BASIS:obs-x\r\n", data)


class Dst(unittest.TestCase):
    def test_wall_clock_end_across_fall_back(self):
        text = build([rec(date="2026-11-01", start_time="00:30")], OPTS).decode()
        self.assertIn("DTEND;TZID=America/Denver:20261101T033000", text)
        self.assertIn("-DTEND-BASIS:default-duration-3h-assumed", text)

    def test_spring_forward_gap_time_is_emitted_unchanged(self):
        data = build([rec(date="2026-03-08", start_time="02:30")], OPTS)
        self.assertIn(b"DTSTART;TZID=America/Denver:20260308T023000\r\n", data)
        self.assertEqual(validate(data)[0], [])


class Cli(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.dir = self._td.name

    def path(self, name):
        return os.path.join(self.dir, name)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def write_feed(self, name="f.ics", **kw):
        p = self.path(name)
        with open(p, "wb") as f:
            f.write(build([rec(), rec(record_id="2", status="cancelled")], OPTS))
        return p

    def test_check_feed_ok(self):
        p = self.write_feed()
        code, out, _ = self.run_cli("check-feed", p, "--expect-events", "2")
        self.assertEqual((code, out), (0, "%s: events=2 failures=0\n" % p))

    def test_check_feed_failures(self):
        p = self.write_feed()
        code, out, _ = self.run_cli("check-feed", p, "--expect-events", "3")
        self.assertEqual(code, 1)
        self.assertEqual(out.splitlines(), ["FAIL: expected 3 events, found 2", "%s: events=2 failures=1" % p])
        with open(p, "ab") as f:
            f.write(b"broken")
        code, out, _ = self.run_cli("check-feed", p)
        self.assertEqual(code, 1)
        self.assertRegex(out.splitlines()[-1], r"events=\d+ failures=[1-9]")

    def test_check_feed_missing_file(self):
        code, _, err = self.run_cli("check-feed", self.path("nope.ics"))
        self.assertEqual(code, 2)
        self.assertTrue(err.startswith("meeting_ics: error: "))

    def test_template(self):
        from importlib import resources
        text = resources.files("meeting_ics.adapters").joinpath("csv_template.csv").read_text(encoding="utf-8")
        cols = text.rstrip("\n").split(",")
        self.assertEqual(text, ",".join(cols) + "\n")
        self.assertEqual(cols[:4], ["record_id", "jurisdiction_slug", "body_slug", "body_name"])
        self.assertEqual(cols[-1], "retrieved_at")
        self.assertEqual(len(cols), 19)
        code, out, _ = self.run_cli("template")
        self.assertEqual((code, out), (0, text))
        p = self.path("t.csv")
        self.assertEqual(self.run_cli("template", "--output", p)[0], 0)
        with open(p, encoding="utf-8", newline="") as f:
            self.assertEqual(f.read(), text)
        code, _, err = self.run_cli("template", "--output", p)
        self.assertEqual((code, err), (2, "meeting_ics: error: refusing to overwrite %s\n" % p))

    def test_build_writes_no_tmp_files_and_unknown_config_key_fails(self):
        inp = self.path("in.json")
        with open(inp, "w", encoding="utf-8") as f:
            f.write('[{"uid_namespace":"t","record_id":"1","body_name":"Board","meeting_type":"Board Meeting",'
                    '"date":"2026-03-01","start_time":"18:00","timezone":"America/Denver","status":"scheduled",'
                    '"retrieved_at":"2026-01-01T00:00:00Z"}]')
        cfg = self.path("c.json")
        with open(cfg, "w", encoding="utf-8") as f:
            f.write('{"uid_domain":"example.org","token":"x"}')
        args = ["build", "--input", inp, "--output", self.path("o/f.ics"), "--ledger", self.path("o/l.json")]
        code, _, err = self.run_cli(*args, "--config", cfg)
        self.assertEqual((code, err), (2, "meeting_ics: error: unknown option(s): token\n"))
        self.assertFalse(os.path.exists(self.path("o")))
        with open(cfg, "w", encoding="utf-8") as f:
            f.write('{"uid_domain":"example.org"}')
        self.assertEqual(self.run_cli(*args, "--config", cfg)[0], 0)
        self.assertEqual(sorted(os.listdir(self.path("o"))), ["f.ics", "l.json"])

    def test_build_warns_about_placeholder_uid_domain(self):
        inp = self.path("in.json")
        with open(inp, "w", encoding="utf-8") as f:
            f.write('[{"uid_namespace":"t","record_id":"1","body_name":"Board","meeting_type":"Board Meeting",'
                    '"date":"2026-03-01","start_time":"18:00","timezone":"America/Denver","status":"scheduled",'
                    '"retrieved_at":"2026-01-01T00:00:00Z"}]')
        code, _, err = self.run_cli("build", "--input", inp, "--output", self.path("f.ics"),
                                    "--ledger", self.path("l.json"))
        self.assertEqual(code, 0)
        self.assertIn("warning: uid_domain is example.invalid", err)


if __name__ == "__main__":
    unittest.main()
