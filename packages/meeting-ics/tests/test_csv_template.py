"""CSV adapter tests (meeting_ics.adapters.csv_template). All data is synthetic."""
import contextlib
import io
import os
import shutil
import tempfile
import unittest
import zoneinfo

from meeting_ics import BuildOptions, RecordError, build, cli
from meeting_ics.adapters import csv_template as ct
from meeting_ics.feedset import build_feedset
from meeting_ics.ledger import Ledger
from meeting_ics.validate import ERROR_CAP

HEAD = ",".join(ct.TEMPLATE_COLUMNS)
STAMP = "2026-11-24T12:00:00Z"


def _q(v):
    return '"%s"' % v.replace('"', '""') if any(ch in v for ch in ',"\n\r') else v


def row(**kw):
    d = dict(record_id="m1", jurisdiction_slug="demo-town", body_slug="town-council", body_name="Town Council",
             meeting_type="Regular Session", date="2026-12-01", start_time="18:00", end_time="",
             timezone="America/Denver", status="scheduled", location="Town Hall", join_url="", dial_in="",
             portal_url="https://demo.example/m/1", agenda_url="", packet_url="", minutes_url="", video_url="",
             retrieved_at=STAMP)
    d.update(kw)
    return ",".join(_q(d[c]) for c in ct.TEMPLATE_COLUMNS)


def csv_text(*rows, head=HEAD):
    return "\n".join((head,) + rows) + "\n"


def conv(text, **kw):
    kw.setdefault("uid_namespace", "demo")
    return ct.convert(text, **kw)


class CsvCase(unittest.TestCase):
    def errs(self, text, **kw):
        with self.assertRaises(RecordError) as cm:
            conv(text, **kw)
        return [(e.path, e.message) for e in cm.exception.errors]

    def one(self, text, path, substr, **kw):
        got = self.errs(text, **kw)
        self.assertTrue(any(p == path and substr in m for p, m in got), got)
        return got

    def tmpdir(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        return d


class TestHappy(CsvCase):
    def test_template_header_matches_shipped_file(self):
        with open(os.path.join(os.path.dirname(ct.__file__), "csv_template.csv"), encoding="utf-8", newline="") as f:
            self.assertEqual(f.read(), HEAD + "\n")

    def test_basic_record(self):
        r, = conv(csv_text(row(end_time="19:30", join_url="https://v.example/j/1", dial_in="+1 555 0100, ID 1",
                               agenda_url="https://demo.example/a/1")))
        self.assertEqual((r.uid_namespace, r.record_id, r.body, r.body_name), ("demo", "m1", "Town Council",
                                                                              "Town Council"))
        self.assertEqual((r.jurisdiction_slug, r.body_slug), ("demo-town", "town-council"))
        self.assertEqual(r.end_time, "19:30")
        self.assertEqual(r.links, {"portal": "https://demo.example/m/1", "agenda": "https://demo.example/a/1"})
        self.assertEqual(r.remote_access, {"join_url": "https://v.example/j/1", "dial_in": "+1 555 0100, ID 1"})
        self.assertEqual(r.retrieved_at, STAMP)
        self.assertEqual(r.provenance, {"adapter": "csv_template", "row": 2})

    def test_provenance_row_counts_physical_lines_and_skips(self):
        text = csv_text(row(record_id="a", location="Line1\nLine2"), "", row(record_id="#skip"),
                        ",".join([""] * 19), row(record_id="b"))
        a, b = conv(text)
        self.assertEqual(a.location, "Line1\nLine2")
        self.assertEqual((a.provenance["row"], b.provenance["row"]), (2, 7))

    def test_blank_then_skip_rows_after_data_row(self):
        text = csv_text(row(), "", row(record_id="#skip"))
        r, = conv(text)
        self.assertEqual(r.provenance, {"adapter": "csv_template", "row": 2})

    def test_normalizations(self):
        r, = conv(csv_text(row(start_time="9:30", end_time="11:00:00", status=" Canceled ")))
        self.assertEqual((r.start_time, r.end_time, r.status), ("09:30", "11:00", "cancelled"))

    def test_status_case_and_aliases(self):
        for s in ("SCHEDULED", "Rescheduled", "postponed", "completed", "confirmed"):
            self.assertEqual(conv(csv_text(row(status=s)))[0].status, s.lower())

    def test_bom_crlf_comment_column_and_optional_columns_omitted(self):
        head = ("record_id,jurisdiction_slug,body_slug,body_name,meeting_type,date,start_time,timezone,status,"
                "#notes,retrieved_at")
        text = ("﻿" + head + "\r\nm1,demo-town,council,Council,Regular,2026-12-01,18:00,America/Denver,"
                "scheduled,hello," + STAMP + "\r\n")
        r, = conv(text)
        self.assertEqual((r.record_id, r.links, r.remote_access, r.location), ("m1", {}, None, None))

    def test_quoted_crlf_inside_location_becomes_lf(self):
        r, = conv(csv_text(row(location="A\r\nB")).replace("\n", "\r\n", 1))
        self.assertEqual(r.location, "A\nB")

    def test_retrieved_at_fallback_and_cell_wins(self):
        a, b = conv(csv_text(row(record_id="a", retrieved_at=""),
                             row(record_id="b", retrieved_at="2026-01-02T03:04:05Z")),
                    retrieved_at="2026-06-01T00:00:00Z")
        self.assertEqual((a.retrieved_at, b.retrieved_at), ("2026-06-01T00:00:00Z", "2026-01-02T03:04:05Z"))

    def test_header_only_returns_empty(self):
        self.assertEqual(conv(HEAD + "\n"), [])

    def test_utf8_text_preserved(self):
        r, = conv(csv_text(row(body_name="Conseil municipal", meeting_type="Réunion ☃")))
        self.assertEqual(r.meeting_type, "Réunion ☃")

    def test_several_timezones_in_one_jurisdiction_allowed(self):
        self.assertEqual(len(conv(csv_text(row(record_id="a"), row(record_id="b", timezone="America/Chicago")))), 2)


class TestHeader(CsvCase):
    def test_duplicate_unknown_missing(self):
        got = self.errs("record_id,record_id,bogus,date\nx,x,x,x\n")
        self.assertEqual({p for p, _ in got}, {"<header>"})
        msgs = " | ".join(m for _, m in got)
        self.assertIn("duplicate column 'record_id'", msgs)
        self.assertIn("unknown column(s): 'bogus' (prefix a column with # to ignore it)", msgs)
        self.assertIn("missing required column(s): jurisdiction_slug", msgs)
        self.assertIn("(see: meeting_ics template)", msgs)

    def test_header_names_are_exact_case(self):
        self.one(csv_text(row(), head=HEAD.replace("record_id", "Record_ID", 1)), "<header>", "unknown column")

    def test_empty_file(self):
        self.one("", "<header>", "empty")
        self.one("\n\n", "<header>", "empty")

    def test_blank_lines_above_header_ok(self):
        self.assertEqual(len(conv("\n" + csv_text(row()))), 1)

    def test_header_error_stops_row_checks(self):
        self.assertTrue(all(p == "<header>" for p, _ in self.errs("record_id,bogus\n,\n")))


class TestRows(CsvCase):
    def test_required_blank_reported_once_per_column(self):
        got = self.errs(csv_text(row(date="", body_name="")))
        self.assertEqual(sorted(got), [("row 2.body_name", "is required"), ("row 2.date", "is required")])

    def test_retrieved_at_blank_without_fallback(self):
        self.assertEqual(self.errs(csv_text(row(retrieved_at=""))),
                         [("row 2.retrieved_at", "is blank and --retrieved-at was not given")])

    def test_bad_retrieved_at_param(self):
        with self.assertRaises(RecordError) as cm:
            conv(csv_text(row()), retrieved_at="yesterday")
        self.assertEqual(cm.exception.path, "--retrieved-at")

    def test_duplicate_record_id(self):
        got = self.one(csv_text(row(), row(portal_url="")), "row 3.record_id", "duplicates record_id 'm1' on row 2")
        self.assertEqual(len(got), 1)

    def test_field_errors_use_row_paths_and_column_names(self):
        text = csv_text(row(date="12/1/2026", portal_url="ftp://x", join_url="nope", status="meh",
                            body_slug="Bad_Slug", start_time="6pm", timezone="Mars/Base"))
        self.assertEqual({p for p, _ in self.errs(text)},
                         {"row 2.date", "row 2.portal_url", "row 2.join_url", "row 2.status", "row 2.body_slug",
                          "row 2.start_time", "row 2.timezone"})

    def test_unknown_status_is_hard_error(self):
        self.one(csv_text(row(status="tentative")), "row 2.status", "unknown status")

    def test_bad_times(self):
        for t in ("18:00:30", "6:00 PM", "24:00", "6.30"):
            self.one(csv_text(row(start_time=t)), "row 2.start_time", "HH:MM")
        self.one(csv_text(row(end_time="18:00")), "row 2.end_time", "after start_time")

    def test_errors_across_rows_are_all_collected(self):
        got = self.errs(csv_text(row(record_id="a", date="x"), row(record_id="b"), row(record_id="c", status="no")))
        self.assertEqual([p for p, _ in got], ["row 2.date", "row 4.status"])

    def test_error_line_has_row_path_and_record_id(self):
        with self.assertRaises(RecordError) as cm:
            conv(csv_text(row(date="x")))
        line = cm.exception.errors[0].line()
        self.assertTrue(line.startswith("row 2.date: "), line)
        self.assertTrue(line.endswith("(record_id 'm1')"), line)

    def test_extra_cells_and_unnamed_column_values(self):
        self.one(csv_text(row() + ",extra"), "row 2", "more cells")
        self.one(HEAD + ",\n" + row() + ",oops\n", "row 2", "unnamed column")
        self.assertEqual(len(conv(HEAD + ",\n" + row() + ",\n")), 1)

    def test_short_row_treated_as_blank_cells(self):
        self.one(HEAD + "\nm1,demo-town\n", "row 2.body_slug", "is required")

    def test_control_character(self):
        self.one(csv_text(row(meeting_type="a\x07b")), "row 2.meeting_type", "control character")

    def test_error_cap(self):
        got = self.errs(csv_text(*[row(record_id="r%d" % i, date="x") for i in range(ERROR_CAP + 20)]))
        self.assertEqual(len(got), ERROR_CAP + 1)
        self.assertIn("too many errors", got[-1][1])


class TestLoad(CsvCase):
    def write(self, data):
        p = os.path.join(self.tmpdir(), "in.csv")
        with open(p, "wb") as f:
            f.write(data)
        return p

    def test_load_with_bom(self):
        p = self.write(b"\xef\xbb\xbf" + csv_text(row()).encode())
        self.assertEqual(len(ct.load(p, uid_namespace="demo")), 1)

    def test_not_utf8(self):
        p = self.write(csv_text(row(location="café")).encode("cp1252"))
        with self.assertRaises(RecordError) as cm:
            ct.load(p, uid_namespace="demo")
        self.assertEqual(cm.exception.path, "<input>")
        self.assertEqual(cm.exception.message, "is not UTF-8; in Excel use Save As > CSV UTF-8")

    def test_missing_file_does_not_leak_path(self):
        with self.assertRaises(RecordError) as cm:
            ct.load("/nonexistent/x.csv", uid_namespace="demo")
        self.assertEqual(cm.exception.path, "<input>")
        self.assertNotIn("nonexistent", cm.exception.message)


class TestPipeline(CsvCase):
    """Adapter output goes through build, build-set and the CLI unchanged."""

    def setUp(self):
        self.tmp = self.tmpdir()
        self.csv = os.path.join(self.tmp, "m.csv")
        with open(self.csv, "w", encoding="utf-8", newline="") as f:
            f.write(csv_text(row(record_id="a"),
                             row(record_id="b", body_slug="planning", body_name="Planning", date="2026-12-02",
                                 status="canceled", retrieved_at="")))
        self.cfg = os.path.join(self.tmp, "c.json")
        with open(self.cfg, "w") as f:
            f.write('{"uid_domain": "example.org"}')

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(list(args))
        return rc, out.getvalue(), err.getvalue()

    def base(self, *extra):
        return ["--input", self.csv, "--adapter", "csv_template", "--uid-namespace", "demo", *extra]

    def test_build_reads_csv_and_is_deterministic(self):
        opts = BuildOptions(uid_domain="example.org")
        a = build(ct.load(self.csv, uid_namespace="demo", retrieved_at=STAMP), opts, Ledger())
        b = build(ct.load(self.csv, uid_namespace="demo", retrieved_at=STAMP), opts, Ledger())
        self.assertEqual(a, b)
        self.assertIn(b"UID:demo-a@example.org", a)
        self.assertIn(b"STATUS:CANCELLED", a)

    def test_build_set_reads_csv(self):
        recs = ct.load(self.csv, uid_namespace="demo", retrieved_at=STAMP)
        fs = build_feedset(recs, BuildOptions(uid_domain="example.org"), Ledger(), {"demo-town": "Demo Town"})
        self.assertEqual(fs.index["totals"], {"cancelled": 1, "events": 2, "feeds": 3})

    def test_cli_validate_build_and_build_set(self):
        rc, out, err = self.run_cli("validate", *self.base("--retrieved-at", STAMP))
        self.assertEqual((rc, out, err), (0, "ok records=2\n", ""))
        rc, out, err = self.run_cli("build", *self.base("--retrieved-at", STAMP), "--config", self.cfg,
                                    "--output", os.path.join(self.tmp, "o.ics"),
                                    "--ledger", os.path.join(self.tmp, "l.json"))
        self.assertEqual((rc, out, err), (0, "events=2 cancelled=1 sequence_bumped=none\n", ""))
        rc, out, err = self.run_cli("build-set", *self.base("--retrieved-at", STAMP), "--config", self.cfg,
                                    "--out", os.path.join(self.tmp, "out"),
                                    "--ledger", os.path.join(self.tmp, "l2.json"))
        self.assertEqual((rc, out, err), (0, "feeds=3 events=2 cancelled=1 sequence_bumped=none\n", ""))

    def test_cli_second_run_is_byte_identical_with_no_bumps(self):
        args = ["build-set", *self.base("--retrieved-at", STAMP), "--config", self.cfg,
                "--ledger", os.path.join(self.tmp, "l.json")]
        self.assertEqual(self.run_cli(*args, "--out", os.path.join(self.tmp, "o1"))[0], 0)
        rc, out, _ = self.run_cli(*args, "--out", os.path.join(self.tmp, "o2"))
        self.assertEqual((rc, out), (0, "feeds=3 events=2 cancelled=1 sequence_bumped=none\n"))
        f = os.path.join("calendar", "demo-town", "all.ics")
        with open(os.path.join(self.tmp, "o1", f), "rb") as a, open(os.path.join(self.tmp, "o2", f), "rb") as b:
            self.assertEqual(a.read(), b.read())

    def test_cli_retrieved_at_now(self):
        rc, out, err = self.run_cli("validate", *self.base("--retrieved-at", "now"))
        self.assertEqual((rc, out), (0, "ok records=2\n"), err)

    def test_cli_retrieved_at_only_with_csv_template(self):
        rc, out, err = self.run_cli("validate", "--input", self.csv, "--retrieved-at", STAMP)
        self.assertEqual((rc, out), (2, ""))
        self.assertEqual(err, "meeting_ics: error: --retrieved-at applies only to --adapter csv_template\n")
        rc, _, err = self.run_cli("validate", "--input", self.csv, "--adapter", "primegov_normalized",
                                  "--uid-namespace", "x", "--retrieved-at", STAMP)
        self.assertEqual(rc, 2)
        self.assertIn("--retrieved-at applies only", err)

    def test_cli_row_errors_printed_and_nothing_written(self):
        rc, out, err = self.run_cli("build", *self.base(), "--output", os.path.join(self.tmp, "o.ics"),
                                    "--ledger", os.path.join(self.tmp, "l.json"))
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("row 3.retrieved_at: is blank and --retrieved-at was not given", err)
        self.assertTrue(err.endswith("meeting_ics: 1 error(s); nothing written\n"), err)
        self.assertEqual(sorted(os.listdir(self.tmp)), ["c.json", "m.csv"])

    def test_cli_bad_retrieved_at(self):
        rc, _, err = self.run_cli("validate", *self.base("--retrieved-at", "2026-13-01"))
        self.assertEqual(rc, 2)
        self.assertIn("--retrieved-at:", err)

    def test_template_command_output_loads_as_empty_and_validate_rejects_empty(self):
        out_path = os.path.join(self.tmp, "t.csv")
        self.assertEqual(self.run_cli("template", "--output", out_path)[0], 0)
        self.assertEqual(ct.load(out_path, uid_namespace="demo"), [])
        rc, _, err = self.run_cli("validate", "--input", out_path, "--adapter", "csv_template",
                                  "--uid-namespace", "demo")
        self.assertEqual(rc, 2)
        self.assertIn("no records: refusing to build an empty feed", err)

    def test_wall_clock_times_survive_dst_day(self):
        if "America/Denver" not in zoneinfo.available_timezones():
            self.skipTest("no tz data")
        recs = conv(csv_text(row(date="2026-03-08", start_time="9:00")))
        data = build(recs, BuildOptions(uid_domain="example.org"), Ledger())
        self.assertIn(b"DTSTART;TZID=America/Denver:20260308T090000", data)


if __name__ == "__main__":
    unittest.main()
