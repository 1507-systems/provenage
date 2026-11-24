"""Tests for the feed-set generator (meeting_ics.feedset) and the build-set command."""
import contextlib
import copy
import hashlib
import io
import json
import os
import re
import shutil
import tempfile
import unittest

from meeting_ics import BuildOptions, Ledger, RecordError, build_feedset, cli
from meeting_ics.check import parse_ics
from meeting_ics.feedset import encode_index, load_set_config, write_feedset
from meeting_ics.validate import SLUG_RE, validate_records

OPTS = BuildOptions(uid_domain="example.org")
NAMES = {"demo-town": "Demo Town"}


def base(rid, body, slug, date, start, mtype, status="scheduled", j="demo-town", **kw):
    d = {"uid_namespace": "demo", "record_id": rid, "body_name": body, "meeting_type": mtype, "date": date,
         "start_time": start, "timezone": "America/Denver", "status": status, "retrieved_at": "2026-03-01T06:00:00Z",
         "jurisdiction_slug": j, "body_slug": slug}
    d.update(kw)
    return d


def fixture():
    return [
        base("CC1", "City Council", "city-council", "2026-03-02", "18:00", "City Council Regular Meeting"),
        base("CC2", "City Council", "city-council", "2026-03-16", "18:00", "City Council Study Session"),
        base("PC1", "Planning Commission", "planning-commission", "2026-03-09", "19:00", "Regular Meeting",
             status="cancelled", retrieved_at="2026-03-05T06:00:00Z"),
    ]


def uid(rid):
    return "demo-%s@example.org" % rid


def vevents(data):
    text = data.decode("utf-8")
    return re.findall(r"BEGIN:VEVENT\r\n.*?END:VEVENT\r\n", text, re.S)


def uids(data):
    return [e.get("UID") for e in parse_ics(data).events]


def feeds(fs):
    return {f.key: f.data for f in fs.feeds}


class Grouping(unittest.TestCase):
    def setUp(self):
        self.fs = build_feedset(fixture(), OPTS, None, NAMES)
        self.f = feeds(self.fs)

    def test_keys_and_counts(self):
        self.assertEqual(sorted(self.f), ["calendar/demo-town/all.ics", "calendar/demo-town/city-council.ics",
                                          "calendar/demo-town/planning-commission.ics"])
        self.assertEqual([len(vevents(self.f[k])) for k in sorted(self.f)], [3, 2, 1])

    def test_body_events_appear_byte_for_byte_in_combined(self):
        allf = self.f["calendar/demo-town/all.ics"].decode()
        body_events = []
        for k in ("city-council", "planning-commission"):
            for e in vevents(self.f["calendar/demo-town/%s.ics" % k]):
                self.assertIn(e, allf)
                body_events.append(e)
        self.assertEqual(len(body_events), len(vevents(self.f["calendar/demo-town/all.ics"])))

    def test_summaries(self):
        for k in ("all", "planning-commission"):
            text = self.f["calendar/demo-town/%s.ics" % k].decode()
            self.assertIn("SUMMARY:Planning Commission Regular Meeting\r\n", text)
        self.assertIn("SUMMARY:City Council Study Session\r\n", self.f["calendar/demo-town/all.ics"].decode())

    def test_sort_order_date_time_uid(self):
        self.assertEqual(uids(self.f["calendar/demo-town/all.ics"]), [uid("CC1"), uid("PC1"), uid("CC2")])

    def test_sort_ties_broken_by_uid(self):
        recs = [base("B", "Board", "board", "2026-03-02", "18:00", "Board Meeting"),
                base("A", "Board", "board", "2026-03-02", "18:00", "Board Meeting")]
        fs = build_feedset(recs, OPTS)
        self.assertEqual(uids(feeds(fs)["calendar/demo-town/board.ics"]), [uid("A"), uid("B")])

    def test_cancelled(self):
        for k in ("all", "planning-commission"):
            self.assertIn("STATUS:CANCELLED", self.f["calendar/demo-town/%s.ics" % k].decode())
        by_key = {e["key"]: e for e in self.fs.index["feeds"]}
        self.assertEqual(by_key["calendar/demo-town/planning-commission.ics"]["cancelled"], 1)
        self.assertEqual(by_key["calendar/demo-town/all.ics"]["cancelled"], 1)
        self.assertEqual(self.fs.index["totals"], {"cancelled": 1, "events": 3, "feeds": 3})

    def test_titles(self):
        cc = self.f["calendar/demo-town/city-council.ics"].decode()
        self.assertIn("X-WR-CALNAME:Demo Town: City Council\r\n", cc)
        self.assertIn("X-WR-CALNAME:Demo Town: All meetings\r\n", self.f["calendar/demo-town/all.ics"].decode())
        plain = feeds(build_feedset(fixture(), OPTS))
        self.assertIn("X-WR-CALNAME:demo-town: City Council\r\n", plain["calendar/demo-town/city-council.ics"].decode())

    def test_two_jurisdictions(self):
        recs = fixture() + [base("X1", "Board", "board", "2026-04-01", "09:00", "Board Meeting", j="other-city")]
        fs = build_feedset(recs, OPTS)
        keys = [f.key for f in fs.feeds]
        self.assertIn("calendar/demo-town/all.ics", keys)
        self.assertIn("calendar/other-city/all.ics", keys)
        self.assertEqual(fs.index["totals"]["events"], 4)

    def test_every_feed_passes_check(self):
        from meeting_ics import check
        for fd in self.fs.feeds:
            self.assertEqual(check.validate(fd.data, fd.events)[0], [])

    def test_headers_have_publish_defaults(self):
        text = self.f["calendar/demo-town/all.ics"].decode()
        for line in ("METHOD:PUBLISH", "REFRESH-INTERVAL;VALUE=DURATION:PT1H", "X-PUBLISHED-TTL:PT1H"):
            self.assertIn(line + "\r\n", text)


class Determinism(unittest.TestCase):
    def test_fresh_runs_identical(self):
        a = build_feedset(fixture(), OPTS, None, NAMES)
        b = build_feedset(fixture(), OPTS, None, NAMES)
        self.assertEqual(feeds(a), feeds(b))
        self.assertEqual(a.index_bytes, b.index_bytes)
        self.assertEqual(a.ledger.dumps(), b.ledger.dumps())

    def test_second_run_with_ledger_has_no_bumps(self):
        a = build_feedset(fixture(), OPTS, None, NAMES)
        b = build_feedset(fixture(), OPTS, a.ledger, NAMES)
        self.assertEqual(feeds(a), feeds(b))
        self.assertEqual(a.ledger.dumps(), b.ledger.dumps())
        self.assertEqual(b.bumped, [])

    def test_input_order_independent(self):
        a = build_feedset(fixture(), OPTS, None, NAMES)
        b = build_feedset(list(reversed(fixture())), OPTS, None, NAMES)
        self.assertEqual(feeds(a), feeds(b))
        self.assertEqual(a.index_bytes, b.index_bytes)
        self.assertEqual(a.ledger.dumps(), b.ledger.dumps())

    def test_time_change_bumps_in_both_feeds(self):
        a = build_feedset(fixture(), OPTS, None, NAMES)
        recs = fixture()
        recs[0]["start_time"] = "18:30"
        recs[0]["retrieved_at"] = "2026-03-10T06:00:00Z"
        b = build_feedset(recs, OPTS, a.ledger, NAMES)
        self.assertEqual(b.bumped, [uid("CC1")])
        for k in ("city-council", "all"):
            cal = parse_ics(feeds(b)["calendar/demo-town/%s.ics" % k])
            self.assertEqual(cal.event(uid("CC1")).get("SEQUENCE"), "1")
            self.assertEqual(cal.event(uid("CC2")).get("SEQUENCE"), "0")

    def test_move_to_another_body_keeps_uid_and_bumps_once(self):
        a = build_feedset(fixture(), OPTS, None, NAMES)
        recs = fixture()
        recs[0].update(body_name="Planning Commission", body_slug="planning-commission")
        b = build_feedset(recs, OPTS, a.ledger, NAMES)
        f = feeds(b)
        self.assertNotIn(uid("CC1"), uids(f["calendar/demo-town/city-council.ics"]))
        pc = parse_ics(f["calendar/demo-town/planning-commission.ics"])
        self.assertEqual(pc.event(uid("CC1")).get("SEQUENCE"), "1")
        self.assertEqual(parse_ics(f["calendar/demo-town/all.ics"]).event(uid("CC1")).get("SEQUENCE"), "1")
        self.assertEqual(b.bumped, [uid("CC1")])

    def test_caller_ledger_not_mutated(self):
        led = build_feedset(fixture(), OPTS).ledger
        before = copy.deepcopy(led.entries)
        recs = fixture()
        recs[0]["start_time"] = "18:30"
        build_feedset(recs, OPTS, led)
        self.assertEqual(led.entries, before)
        bad = fixture() + [base("Z", "Board", "board", "2026-05-01", "09:00", "Board", timezone="Not/AZone")]
        with self.assertRaises(ValueError):
            build_feedset(bad, OPTS, led)
        self.assertEqual(led.entries, before)


class Index(unittest.TestCase):
    def test_index_shape(self):
        fs = build_feedset(fixture(), OPTS, None, NAMES)
        idx = json.loads(fs.index_bytes)
        self.assertEqual(idx["schema"], "provenage-calendar-index/1")
        self.assertEqual(idx["generated_at"], "2026-03-05T06:00:00Z")
        self.assertEqual([e["key"] for e in idx["feeds"]], sorted(e["key"] for e in idx["feeds"]))
        data = feeds(fs)
        for e in idx["feeds"]:
            self.assertEqual(e["href"], e["key"][len("calendar/"):])
            self.assertEqual(e["size"], len(data[e["key"]]))
            self.assertEqual(e["sha256"], hashlib.sha256(data[e["key"]]).hexdigest())
            self.assertEqual(set(e), {"body", "body_name", "cancelled", "events", "href", "jurisdiction", "key",
                                      "kind", "sha256", "size", "title"})
        allent = [e for e in idx["feeds"] if e["kind"] == "combined"][0]
        self.assertEqual((allent["body"], allent["body_name"], allent["title"], allent["events"]),
                         ("all", None, "Demo Town: All meetings", 3))
        cc = [e for e in idx["feeds"] if e["body"] == "city-council"][0]
        self.assertEqual((cc["kind"], cc["body_name"], cc["title"]),
                         ("body", "City Council", "Demo Town: City Council"))
        self.assertTrue(fs.index_bytes.endswith(b"\n"))
        self.assertEqual(fs.index_bytes, encode_index(fs.index))
        self.assertEqual(set(idx), {"feeds", "generated_at", "schema", "totals"})


class Rejections(unittest.TestCase):
    def build(self, **kw):
        recs = fixture()
        recs[0].update(kw)
        return build_feedset(recs, OPTS)

    def test_missing_slugs(self):
        recs = fixture()
        del recs[0]["jurisdiction_slug"]
        del recs[0]["body_slug"]
        with self.assertRaisesRegex(ValueError, r"record demo-CC1@example.org: jurisdiction_slug is required"):
            build_feedset(recs, OPTS)
        recs = fixture()
        del recs[0]["body_slug"]
        with self.assertRaisesRegex(ValueError, r"record demo-CC1@example.org: body_slug is required for build-set"):
            build_feedset(recs, OPTS)

    def test_bad_slug_is_record_error(self):
        with self.assertRaises(RecordError):
            self.build(body_slug="City Council")
        with self.assertRaises(RecordError):
            self.build(body_slug="a.b")
        with self.assertRaises(RecordError):
            self.build(body_slug="../x")

    def test_all_reserved(self):
        with self.assertRaisesRegex(ValueError, "body_slug 'all' is reserved for the combined feed"):
            self.build(body_slug="all")

    def test_conflicting_body_names(self):
        with self.assertRaisesRegex(ValueError, "body_slug 'city-council' in 'demo-town' has conflicting body names: "
                                                "'City Council', 'City Council Board'"):
            extra = base("CC3", "City Council Board", "city-council", "2026-04-01", "18:00", "X")
            build_feedset(fixture() + [extra], OPTS)

    def test_duplicate_uid(self):
        recs = fixture()
        recs[2].update(record_id="CC1", uid_namespace="demo")
        with self.assertRaises(RecordError):
            build_feedset(recs, OPTS)

    def test_uid_collision_across_namespace_split(self):
        recs = [base("b-c", "Board", "board", "2026-03-02", "18:00", "Board", uid_namespace="a"),
                base("c", "Board", "board", "2026-03-03", "18:00", "Board", uid_namespace="a-b")]
        with self.assertRaisesRegex(ValueError, "duplicate UID in input: a-b-c@example.org"):
            build_feedset(recs, OPTS)

    def test_empty_input(self):
        with self.assertRaises(RecordError):
            build_feedset([], OPTS)

    def test_config(self):
        with self.assertRaisesRegex(ValueError, "calendar_name is set per feed by build-set"):
            load_set_config({"uid_domain": "example.org", "calendar_name": "x"})
        with self.assertRaisesRegex(ValueError, "build-set requires uid_domain"):
            load_set_config({})
        with self.assertRaisesRegex(ValueError, "unknown option"):
            load_set_config({"uid_domain": "example.org", "token": "x"})
        cfg = {"uid_domain": "example.org", "jurisdictions": {"demo-town": {"name": "Demo Town"}}}
        opts, names = load_set_config(cfg)
        self.assertEqual((opts.uid_domain, names), ("example.org", {"demo-town": "Demo Town"}))
        for bad in ({"Demo": {"name": "x"}}, {"demo-town": {"name": "a\nb"}}, {"demo-town": {"nm": "x"}},
                    {"demo-town": "x"}, []):
            with self.assertRaises(ValueError):
                load_set_config({"uid_domain": "example.org", "jurisdictions": bad})

    def test_invalid_built_feed_is_refused(self):
        from unittest import mock
        with mock.patch("meeting_ics.feedset.check.validate", return_value=(["boom"], [])):
            with self.assertRaisesRegex(ValueError, r"built feed calendar/demo-town/.*\.ics failed validation: boom"):
                build_feedset(fixture(), OPTS)


class SlugVersusWorker(unittest.TestCase):
    def test_slug_re_is_subset_of_worker_segment(self):
        src = os.path.join(os.path.dirname(__file__), "..", "..", "..", "workers", "feeds", "src", "index.ts")
        if not os.path.exists(src):
            self.skipTest("worker source not present")
        m = re.search(r"SEGMENT\s*=\s*'([^']+)'", open(src, encoding="utf-8").read())
        self.assertIsNotNone(m, "SEGMENT literal not found in index.ts")
        seg = re.compile("^" + m.group(1) + "$")
        for s in ("a", "demo-town", "city-council", "a1-b2-c3", "x" * 64):
            self.assertTrue(re.fullmatch(SLUG_RE, s))
            self.assertTrue(seg.match(s), s)
            self.assertLessEqual(len(s), 64)
        for s in (".", "..", "a.b", "a_b", "-a", "a-", "a--b", "A"):
            self.assertFalse(re.fullmatch(SLUG_RE, s), s)
        too_long = "x" * 65
        recs = fixture()
        recs[0]["body_slug"] = too_long
        with self.assertRaises(RecordError):
            validate_records(recs)


class WriteSet(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.fs = build_feedset(fixture(), OPTS, None, NAMES)

    def test_writes_files(self):
        out = os.path.join(self.tmp, "out")
        paths = write_feedset(self.fs, out)
        self.assertEqual(len(paths), 4)
        with open(os.path.join(out, "calendar", "demo-town", "all.ics"), "rb") as f:
            self.assertEqual(f.read(), feeds(self.fs)["calendar/demo-town/all.ics"])
        with open(os.path.join(out, "calendar", "index.json"), "rb") as f:
            self.assertEqual(f.read(), self.fs.index_bytes)

    def test_non_empty_needs_overwrite(self):
        out = os.path.join(self.tmp, "out")
        write_feedset(self.fs, out)
        with self.assertRaisesRegex(ValueError, "is not empty; pass --overwrite"):
            write_feedset(self.fs, out)
        stale = os.path.join(out, "calendar", "stale.txt")
        open(stale, "w").close()
        write_feedset(self.fs, out, overwrite=True)
        self.assertFalse(os.path.exists(stale))

    def test_symlinked_calendar_dir_refused(self):
        out = os.path.join(self.tmp, "out")
        elsewhere = os.path.join(self.tmp, "elsewhere")
        os.makedirs(out)
        os.makedirs(elsewhere)
        try:
            os.symlink(elsewhere, os.path.join(out, "calendar"))
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        for ow in (False, True):
            with self.assertRaisesRegex(ValueError, "is a symlink; refusing"):
                write_feedset(self.fs, out, overwrite=ow)
        self.assertEqual(os.listdir(elsewhere), [])

    def test_target_resolving_outside_is_refused(self):
        # Defence in depth: slugs are validated, but a key that resolves outside out_dir must still be refused,
        # and nothing may be written before every target has been checked.
        from dataclasses import replace
        out = os.path.join(self.tmp, "out")
        elsewhere = os.path.join(self.tmp, "elsewhere")
        os.makedirs(os.path.join(out, "calendar"))
        os.makedirs(elsewhere)
        hostile = replace(self.fs.feeds[0], key="calendar/../../elsewhere/x.ics")
        fs = replace(self.fs, feeds=[self.fs.feeds[1], hostile])
        with self.assertRaisesRegex(ValueError, "refusing to write outside output directory"):
            write_feedset(fs, out)
        self.assertEqual(os.listdir(elsewhere), [])
        self.assertEqual(os.listdir(os.path.join(out, "calendar")), [])


class Cli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.p = lambda n: os.path.join(self.tmp, n)
        with open(self.p("in.json"), "w") as f:
            json.dump(fixture(), f)
        with open(self.p("set.json"), "w") as f:
            json.dump({"uid_domain": "example.org", "jurisdictions": {"demo-town": {"name": "Demo Town"}}}, f)

    def run_cli(self, *extra, config="set.json", input_="in.json"):
        argv = ["build-set", "--input", self.p(input_), "--config", self.p(config), "--ledger", self.p("ledger.json"),
                "--out", self.p("out")] + list(extra)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def tree(self):
        res = {}
        for root, _, files in os.walk(self.p("out")):
            for n in files:
                with open(os.path.join(root, n), "rb") as f:
                    res[os.path.join(root, n)] = f.read()
        return res

    def test_run_rerun_overwrite(self):
        rc, out, err = self.run_cli()
        self.assertEqual((rc, out, err), (0, "feeds=3 events=3 cancelled=1 sequence_bumped=none\n", ""))
        first, ledger = self.tree(), open(self.p("ledger.json"), "rb").read()
        self.assertEqual(len(first), 4)
        rc, out, err = self.run_cli()
        self.assertEqual(rc, 2)
        self.assertIn("meeting_ics: error: output directory", err)
        self.assertEqual(open(self.p("ledger.json"), "rb").read(), ledger)
        rc, out, err = self.run_cli("--overwrite")
        self.assertEqual(rc, 0)
        self.assertEqual(self.tree(), first)
        self.assertEqual(open(self.p("ledger.json"), "rb").read(), ledger)

    def test_bad_records_write_nothing(self):
        recs = fixture()
        recs[1]["status"] = "bogus"
        with open(self.p("bad.json"), "w") as f:
            json.dump(recs, f)
        rc, out, err = self.run_cli(input_="bad.json")
        self.assertEqual(rc, 2)
        self.assertIn("nothing written", err)
        self.assertFalse(os.path.exists(self.p("out")))
        self.assertFalse(os.path.exists(self.p("ledger.json")))

    def test_missing_uid_domain_and_calendar_name(self):
        for cfg, msg in (({}, "build-set requires uid_domain"),
                         ({"uid_domain": "example.org", "calendar_name": "x"}, "calendar_name is set per feed")):
            with open(self.p("c.json"), "w") as f:
                json.dump(cfg, f)
            rc, out, err = self.run_cli(config="c.json")
            self.assertEqual(rc, 2)
            self.assertIn(msg, err)
            self.assertFalse(os.path.exists(self.p("out")))

    def test_bumped_reported(self):
        self.run_cli()
        recs = fixture()
        recs[0]["start_time"] = "18:30"
        with open(self.p("in.json"), "w") as f:
            json.dump(recs, f)
        rc, out, err = self.run_cli("--overwrite")
        self.assertEqual(rc, 0)
        self.assertEqual(out, "feeds=3 events=3 cancelled=1 sequence_bumped=%s\n" % uid("CC1"))
        self.assertIn(uid("CC1"), Ledger.load(self.p("ledger.json")).entries)

    def test_adapter_passthrough(self):
        rc, out, err = self.run_cli("--adapter", "nonexistent", "--uid-namespace", "x")
        self.assertEqual(rc, 2)
        self.assertIn("unknown adapter", err)
        rc, out, err = self.run_cli("--adapter", "primegov_normalized")
        self.assertEqual(rc, 2)
        self.assertIn("--uid-namespace is required with --adapter", err)


if __name__ == "__main__":
    unittest.main()
