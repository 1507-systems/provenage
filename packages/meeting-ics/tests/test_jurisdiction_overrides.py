"""Per-jurisdiction overrides in a feed set (C8): uid_namespace, prodid, calendar_description, default duration
and end rules. A config without overrides is covered byte for byte by test_demo_town.py; the first test here
also proves a set that merely lists jurisdictions without overrides is unchanged."""
import contextlib
import io
import json
import os
import tempfile
import unittest

from meeting_ics import BuildOptions, cli, feedset
from meeting_ics.adapters import csv_template
from meeting_ics.ledger import Ledger

from tests import demo_town
from tests.helpers import rec

STAMP = "2026-10-01T15:00:00Z"
V1 = os.path.join(demo_town.FIXTURES, "meetings-v1.csv")


def demo_text(slug="demo-town"):
    with open(V1, encoding="utf-8") as f:
        return f.read().replace(",demo-town,", ",%s," % slug)


def demo_records(slug="demo-town", ns="demo-town"):
    return csv_template.convert(demo_text(slug), uid_namespace=ns, retrieved_at=STAMP)


def demo_cfg():
    with open(demo_town.OPTIONS, encoding="utf-8") as f:
        return json.load(f)


def feed(fs, key):
    return next(f for f in fs.feeds if f.key == key).data


class Overrides(unittest.TestCase):
    def test_no_overrides_is_identical(self):
        cfg = demo_cfg()
        opts, names, meta = feedset.load_set_meta(cfg)
        self.assertEqual((meta.uid_namespaces, meta.overrides), ({}, {}))
        plain = feedset.build_feedset(demo_records(), opts, None, names)
        withmeta = feedset.build_feedset(demo_records(), opts, None, names, meta)
        self.assertEqual([f.data for f in plain.feeds], [f.data for f in withmeta.feeds])
        self.assertEqual(plain.index_bytes, withmeta.index_bytes)

    def test_demo_town_unchanged_beside_longmont(self):
        """demo-town keeps its namespace, description and rules while the set-wide values differ."""
        orig = demo_cfg()
        cfg = {"uid_domain": orig["uid_domain"], "x_prefix": "X-PROVENAGE", "default_duration_hours": 2,
               "calendar_description": "Set-wide text",
               "jurisdictions": {
                   "demo-town": {"name": "Demo Town", "uid_namespace": "demo-town", "prodid": orig["prodid"],
                                 "calendar_description": orig["calendar_description"],
                                 "default_duration_hours": 3, "end_rules": orig["end_rules"]},
                   "longmont-co": {"name": "Longmont", "uid_namespace": "longmont-co"}}}
        opts, names, meta = feedset.load_set_meta(cfg)
        recs = demo_records() + demo_records("longmont-co", ns="whatever")
        fs = feedset.build_feedset(recs, opts, None, names, meta)
        with open(os.path.join(demo_town.EXPECTED, "v1", "calendar", "demo-town", "all.ics"), "rb") as f:
            golden = f.read()
        self.assertEqual(feed(fs, "calendar/demo-town/all.ics"), golden)
        uids = [ln for ln in feed(fs, "calendar/longmont-co/all.ics").decode().splitlines() if ln.startswith("UID:")]
        self.assertEqual(len(uids), 11)
        self.assertTrue(all(u.startswith("UID:longmont-co-") for u in uids))
        self.assertIn(b"Set-wide text", feed(fs, "calendar/longmont-co/all.ics"))
        self.assertNotIn(b"Fictional demonstration", feed(fs, "calendar/longmont-co/all.ics"))
        self.assertEqual(len(fs.ledger.entries), 22)

    def test_each_override_applies_to_its_jurisdiction_only(self):
        a = rec(jurisdiction_slug="a-xx", body_slug="b", record_id="1", timezone="America/Chicago")
        b = rec(jurisdiction_slug="b-xx", body_slug="b", record_id="2", meeting_type="Study Session")
        cfg = {"uid_domain": "example.org",
               "jurisdictions": {"a-xx": {"name": "A", "prodid": "-//X//A//EN", "calendar_description": "About A",
                                          "default_duration_minutes": 45},
                                 "b-xx": {"name": "B", "default_duration": "PT90M",
                                          "end_rules": [{"match": "study", "duration_minutes": 20}]}}}
        opts, names, meta = feedset.load_set_meta(cfg)
        fs = feedset.build_feedset([a, b], opts, None, names, meta)
        fa, fb = feed(fs, "calendar/a-xx/all.ics"), feed(fs, "calendar/b-xx/all.ics")
        self.assertIn(b"PRODID:-//X//A//EN", fa)
        self.assertIn(b"X-WR-CALDESC:About A", fa)
        self.assertIn(b"T184500", fa)                      # 18:00 + 45 minutes
        self.assertNotIn(b"X-WR-CALDESC", fb)
        self.assertNotIn(b"-//X//A//EN", fb)
        self.assertIn(b"T182000", fb)                      # end rule 20 minutes beats the 90 minute default
        self.assertEqual(opts.default_duration, BuildOptions().default_duration)

    def test_null_description_override_removes_it(self):
        cfg = {"uid_domain": "example.org", "calendar_description": "Set-wide",
               "jurisdictions": {"a-xx": {"name": "A", "calendar_description": None}}}
        opts, names, meta = feedset.load_set_meta(cfg)
        fs = feedset.build_feedset([rec(jurisdiction_slug="a-xx", body_slug="b")], opts, None, names, meta)
        self.assertNotIn(b"X-WR-CALDESC", fs.feeds[0].data)

    def test_invalid_overrides_are_refused_at_load(self):
        base = {"uid_domain": "example.org"}
        for entry, text in (({"uid_namespace": "bad ns"}, "uid_namespace"),
                            ({"uid_namespace": 3}, "uid_namespace"),
                            ({"default_duration_hours": 0}, "default_duration"),
                            ({"prodid": "a;b"}, "prodid"),
                            ({"end_rules": "nope"}, "end_rules"),
                            ({"x_prefix": "X-OTHER"}, "must be an object"),     # not overridable
                            ({"default_duration_hours": 1, "default_duration_minutes": 5}, "only one")):
            with self.subTest(entry=entry):
                with self.assertRaises(ValueError) as cm:
                    feedset.load_set_meta(dict(base, jurisdictions={"a-xx": dict(entry, name="A")}))
                self.assertIn(text, str(cm.exception))

    def test_override_for_unknown_jurisdiction_is_refused(self):
        cfg = {"uid_domain": "example.org",
               "jurisdictions": {"a-xx": {"name": "A"}, "typo-xx": {"name": "T", "uid_namespace": "t"}}}
        opts, names, meta = feedset.load_set_meta(cfg)
        with self.assertRaisesRegex(ValueError, "typo-xx.*no records"):
            feedset.build_feedset([rec(jurisdiction_slug="a-xx", body_slug="b")], opts, None, names, meta)


class Collisions(unittest.TestCase):
    def test_same_uid_in_two_jurisdictions_is_refused(self):
        recs = [rec(jurisdiction_slug="a-xx", body_slug="b"), rec(jurisdiction_slug="b-xx", body_slug="b")]
        with self.assertRaisesRegex(ValueError, "UID collision across jurisdictions.*'a-xx'.*'b-xx'"):
            feedset.build_feedset(recs, BuildOptions(uid_domain="example.org"))

    def test_two_jurisdictions_from_two_csvs_with_one_namespace_are_refused(self):
        recs = demo_records() + demo_records("longmont-co")
        with self.assertRaisesRegex(ValueError, "UID collision across jurisdictions"):
            feedset.build_feedset(recs, BuildOptions(uid_domain="example.org"))

    def test_override_that_creates_a_collision_is_refused(self):
        cfg = {"uid_domain": "example.org",
               "jurisdictions": {"a-xx": {"name": "A", "uid_namespace": "shared"},
                                 "b-xx": {"name": "B", "uid_namespace": "shared"}}}
        opts, names, meta = feedset.load_set_meta(cfg)
        recs = [rec(jurisdiction_slug="a-xx", body_slug="b", uid_namespace="x"),
                rec(jurisdiction_slug="b-xx", body_slug="b", uid_namespace="y")]
        with self.assertRaisesRegex(ValueError, "UID collision"):
            feedset.build_feedset(recs, opts, None, names, meta)

    def test_distinct_namespaces_pass(self):
        recs = [rec(jurisdiction_slug="a-xx", body_slug="b", uid_namespace="a-xx"),
                rec(jurisdiction_slug="b-xx", body_slug="b", uid_namespace="b-xx")]
        fs = feedset.build_feedset(recs, BuildOptions(uid_domain="example.org"))
        self.assertEqual(len(fs.ledger.entries), 2)

    def test_dict_records_are_handled(self):
        d = {"uid_namespace": "t", "record_id": "1", "jurisdiction_slug": "a-xx", "body_slug": "b",
             "body": "Board", "meeting_type": "Board Meeting", "date": "2026-03-01", "start_time": "18:00",
             "timezone": "America/Denver", "status": "confirmed", "retrieved_at": "2026-01-01T00:00:00Z"}
        cfg = {"uid_domain": "example.org", "jurisdictions": {"a-xx": {"name": "A", "uid_namespace": "own"}}}
        opts, names, meta = feedset.load_set_meta(cfg)
        fs = feedset.build_feedset([d, dict(d, jurisdiction_slug="b-xx")], opts, None, names, meta)
        self.assertEqual(sorted(fs.ledger.entries), ["own-1@example.org", "t-1@example.org"])


class LedgerStability(unittest.TestCase):
    def cfg(self, with_longmont):
        c = demo_cfg()
        c["jurisdictions"]["demo-town"]["uid_namespace"] = "demo-town"
        if with_longmont:
            c["jurisdictions"]["longmont-co"] = {"name": "Longmont", "uid_namespace": "longmont-co",
                                                 "calendar_description": "Longmont sample"}
        return feedset.load_set_meta(c)

    def test_adding_a_jurisdiction_leaves_the_existing_one_untouched(self):
        opts, names, meta = self.cfg(False)
        first = feedset.build_feedset(demo_records(), opts, None, names, meta)
        opts2, names2, meta2 = self.cfg(True)
        both = feedset.build_feedset(demo_records() + demo_records("longmont-co"), opts2, first.ledger, names2, meta2)
        self.assertEqual(both.bumped, [])
        for key in ("all", "town-council", "planning-commission"):
            k = "calendar/demo-town/%s.ics" % key
            self.assertEqual(feed(first, k), feed(both, k))
        for uid, entry in first.ledger.entries.items():
            self.assertEqual(both.ledger.entries[uid], entry)
        self.assertEqual(len(both.ledger.entries), 2 * len(first.ledger.entries))

    def test_rebuild_with_the_saved_ledger_is_stable(self):
        opts, names, meta = self.cfg(True)
        recs = demo_records() + demo_records("longmont-co")
        one = feedset.build_feedset(recs, opts, None, names, meta)
        two = feedset.build_feedset(recs, opts, Ledger(one.ledger.entries), names, meta)
        self.assertEqual(two.bumped, [])
        self.assertEqual([f.data for f in one.feeds], [f.data for f in two.feeds])
        self.assertEqual(one.ledger.entries, two.ledger.entries)


class MetaValidation(unittest.TestCase):
    """build_feedset validates a SetMeta that did not come through load_set_meta."""

    def build(self, meta, names=None):
        return feedset.build_feedset([rec(jurisdiction_slug="a-xx", body_slug="b")],
                                     BuildOptions(uid_domain="example.org"), None, names, meta)

    def test_bad_values(self):
        empty = feedset._empty_aliases
        for meta, text in ((feedset.SetMeta({"a-xx": "x" * 41}, {}, empty()), "short_name"),
                           (feedset.SetMeta({"a-xx": "line\nbreak"}, {}, empty()), "short_name"),
                           (feedset.SetMeta({}, {"a-xx": "bogus"}, empty()), "status"),
                           (feedset.SetMeta({}, {}, {"bodies": {"nope": "x"}, "jurisdictions": {}}), "aliases"),
                           (feedset.SetMeta({}, {}, empty(), {"a-xx": "bad ns"}), "uid_namespace"),
                           (feedset.SetMeta({}, {}, empty(), {}, {"a-xx": {"prodid": "a;b"}}), "prodid")):
            with self.subTest(text=text, meta=meta):
                with self.assertRaisesRegex(ValueError, text):
                    self.build(meta)

    def test_bad_name(self):
        with self.assertRaisesRegex(ValueError, "name"):
            self.build(feedset.SetMeta({}, {}, feedset._empty_aliases()), {"a-xx": "two\nlines"})

    def test_good_meta_passes(self):
        fs = self.build(feedset.SetMeta({"a-xx": "A"}, {"a-xx": "dev"}, feedset._empty_aliases()))
        self.assertEqual(fs.index["jurisdictions"]["a-xx"]["status"], "dev")


class MultiInput(unittest.TestCase):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        t = self.tmp.name
        self.p = {}
        for slug in ("demo-town", "longmont-co"):
            self.p[slug] = os.path.join(t, slug + ".csv")
            with open(self.p[slug], "w", encoding="utf-8") as f:
                f.write(demo_text(slug))
        cfg = demo_cfg()
        cfg["jurisdictions"]["demo-town"]["uid_namespace"] = "demo-town"
        cfg["jurisdictions"]["longmont-co"] = {"name": "Longmont", "uid_namespace": "longmont-co"}
        self.cfg = os.path.join(t, "set.json")
        with open(self.cfg, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        self.common = ["--adapter", "csv_template", "--uid-namespace", "shared", "--retrieved-at", STAMP]

    def test_build_set_from_two_csvs(self):
        out = os.path.join(self.tmp.name, "out")
        code, stdout, err = self.run_cli("build-set", *self.common, "--input", self.p["demo-town"],
                                         "--input", self.p["longmont-co"], "--config", self.cfg,
                                         "--ledger", os.path.join(self.tmp.name, "l.json"), "--out", out)
        self.assertEqual(code, 0, err)
        self.assertIn("feeds=6 events=22", stdout)
        self.assertTrue(os.path.isfile(os.path.join(out, "calendar", "longmont-co", "all.ics")))

    def test_without_overrides_the_second_csv_collides(self):
        with open(self.cfg, "w", encoding="utf-8") as f:
            json.dump(dict(demo_cfg(), jurisdictions={"demo-town": {"name": "D"}, "longmont-co": {"name": "L"}}), f)
        code, _o, err = self.run_cli("build-set", *self.common, "--input", self.p["demo-town"],
                                     "--input", self.p["longmont-co"], "--config", self.cfg,
                                     "--ledger", os.path.join(self.tmp.name, "l.json"),
                                     "--out", os.path.join(self.tmp.name, "out"))
        self.assertEqual(code, 2)
        self.assertIn("UID collision across jurisdictions", err)

    def test_errors_name_the_file(self):
        bad = os.path.join(self.tmp.name, "bad.csv")
        with open(bad, "w", encoding="utf-8") as f:
            f.write(demo_text().replace("2026-10-07", "10/7/26", 1))
        code, _o, err = self.run_cli("validate", *self.common, "--input", self.p["longmont-co"], "--input", bad)
        self.assertEqual(code, 2)
        self.assertIn(bad + ": row", err)

    def test_validate_with_config_accepts_what_build_set_accepts(self):
        code, out, err = self.run_cli("validate", *self.common, "--input", self.p["demo-town"],
                                      "--input", self.p["longmont-co"], "--config", self.cfg)
        self.assertEqual((code, out.strip()), (0, "ok records=22"), err)

    def test_validate_without_config_still_reports_the_duplicate(self):
        code, _o, err = self.run_cli("validate", *self.common, "--input", self.p["demo-town"],
                                     "--input", self.p["longmont-co"])
        self.assertEqual(code, 2)
        self.assertIn("duplicate UID", err)

    def test_validate_with_config_reports_cross_jurisdiction_collision(self):
        with open(self.cfg, "w", encoding="utf-8") as f:
            json.dump(dict(demo_cfg(), jurisdictions={"demo-town": {"name": "D"}, "longmont-co": {"name": "L"}}), f)
        code, _o, err = self.run_cli("validate", *self.common, "--input", self.p["demo-town"],
                                     "--input", self.p["longmont-co"], "--config", self.cfg)
        self.assertEqual(code, 2)
        self.assertIn("UID collision across jurisdictions", err)

    def test_validate_with_config_rejects_misspelt_override_slug(self):
        with open(self.cfg, "w", encoding="utf-8") as f:
            json.dump(dict(demo_cfg(), jurisdictions={"longmnt-co": {"name": "L", "uid_namespace": "longmont-co"}}), f)
        args = ("--input", self.p["demo-town"], "--config", self.cfg)
        code, _o, err = self.run_cli("validate", *self.common, *args)
        self.assertEqual(code, 2)
        self.assertIn("no records", err)
        code2, _o, err2 = self.run_cli("build-set", *self.common, *args, "--ledger",
                                       os.path.join(self.tmp.name, "l.json"), "--out", os.path.join(self.tmp.name, "o"))
        self.assertEqual((code2, err2), (2, err))

    def test_validate_with_config_rejects_reserved_body_slug(self):
        bad = os.path.join(self.tmp.name, "all.csv")
        with open(bad, "w", encoding="utf-8") as f:
            f.write(demo_text().replace("planning-commission", "all"))
        code, _o, err = self.run_cli("validate", *self.common, "--input", bad, "--config", self.cfg)
        self.assertEqual(code, 2)
        self.assertIn("all", err)

    def test_single_input_still_works(self):
        code, out, _e = self.run_cli("validate", *self.common, "--input", self.p["demo-town"])
        self.assertEqual((code, out.strip()), (0, "ok records=11"))


if __name__ == "__main__":
    unittest.main()
