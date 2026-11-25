"""Index contract tests (M2 C1): jurisdictions, aliases and mix blocks, and their config validation."""
import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest

from meeting_ics import build_feedset, cli
from meeting_ics.feedset import MIX, load_set_config, load_set_meta

from tests.test_feedset import NAMES, OPTS, base, fixture

CFG = {"uid_domain": "example.org", "jurisdictions": {"demo-town": {"name": "Demo Town"}}}


def build(cfg, recs=None):
    opts, names, meta = load_set_meta(cfg)
    return build_feedset(recs or fixture(), opts, None, names, meta)


class IndexBlocks(unittest.TestCase):
    def test_defaults_match_old_index_plus_three_keys(self):
        old = build_feedset(fixture(), OPTS, None, NAMES)
        idx = old.index
        self.assertEqual(idx["jurisdictions"],
                         {"demo-town": {"name": "Demo Town", "short_name": "Demo Town", "status": "live"}})
        self.assertEqual(idx["aliases"], {"bodies": {}, "jurisdictions": {}})
        self.assertEqual(idx["mix"], {"version": 1, "path": "mix/v1/", "max_refs": 32, "max_selection_bytes": 1536,
                                      "issue_max_bytes": 2000000})
        rest = {k: v for k, v in idx.items() if k not in ("jurisdictions", "aliases", "mix")}
        self.assertEqual(set(rest), {"feeds", "generated_at", "schema", "totals"})
        # feeds are byte-identical whether or not meta is supplied
        new = build(CFG)
        self.assertEqual({f.key: f.data for f in new.feeds}, {f.key: f.data for f in old.feeds})
        self.assertEqual(new.index, old.index)

    def test_no_names_defaults_to_slug(self):
        idx = build_feedset(fixture(), OPTS).index
        self.assertEqual(idx["jurisdictions"]["demo-town"]["name"], "demo-town")

    def test_short_name_and_status(self):
        cfg = {"uid_domain": "example.org", "jurisdictions": {
            "demo-town": {"name": "Demo Town", "short_name": "Demo", "status": "dev"}}}
        j = build(cfg).index["jurisdictions"]["demo-town"]
        self.assertEqual(j, {"name": "Demo Town", "short_name": "Demo", "status": "dev"})

    def test_two_jurisdictions_listed_sorted(self):
        recs = fixture() + [base("X1", "Board", "board", "2026-03-03", "10:00", "Meeting", j="alpha-ville")]
        idx = build(CFG, recs).index
        self.assertEqual(list(idx["jurisdictions"]), ["alpha-ville", "demo-town"])

    def test_bad_short_name_and_status(self):
        def cfg(**kw):
            e = {"name": "Demo Town"}
            e.update(kw)
            return {"uid_domain": "example.org", "jurisdictions": {"demo-town": e}}
        for bad in ({"short_name": "x" * 41}, {"short_name": ""}, {"short_name": "a\nb"}, {"short_name": 5},
                    {"status": "Live"}, {"status": "retired"}, {"status": None}, {"extra": 1}):
            with self.assertRaises(ValueError, msg=repr(bad)):
                load_set_config(cfg(**bad))
        load_set_config(cfg(short_name="x" * 40, status="dev"))
        with self.assertRaises(ValueError):
            load_set_config({"uid_domain": "example.org", "jurisdictions": {"demo-town": {"short_name": "D"}}})

    def test_long_name_without_short_name_refused(self):
        cfg = {"uid_domain": "example.org", "jurisdictions": {"demo-town": {"name": "N" * 41}}}
        with self.assertRaisesRegex(ValueError, "set short_name"):
            build(cfg)
        cfg["jurisdictions"]["demo-town"]["short_name"] = "N"
        build(cfg)


class Aliases(unittest.TestCase):
    def cfg(self, aliases):
        c = dict(CFG)
        c["aliases"] = aliases
        return c

    def test_valid_aliases_published(self):
        a = {"bodies": {"demo-town/old-council": "demo-town/city-council", "demo-town/gone": None},
             "jurisdictions": {"old-town": "demo-town", "dead-town": None}}
        self.assertEqual(build(self.cfg(a)).index["aliases"], a)

    def test_bad_shapes_rejected(self):
        for bad in ([], {"other": {}}, {"bodies": []}, {"bodies": {"Demo/x": "demo-town/city-council"}},
                    {"bodies": {"demo-town": "demo-town/city-council"}},
                    {"bodies": {"a/b": "demo-town"}}, {"bodies": {"a/b/c": None}},
                    {"bodies": {"a/all": None}}, {"bodies": {"a/b": 3}},
                    {"jurisdictions": {"A": "demo-town"}}, {"jurisdictions": {"a/b": "demo-town"}},
                    {"jurisdictions": {"a": "Demo"}}, {"jurisdictions": {"a": 1}}):
            with self.assertRaises(ValueError, msg=repr(bad)):
                load_set_config(self.cfg(bad))

    def test_source_may_not_be_current(self):
        with self.assertRaisesRegex(ValueError, "current feed"):
            build(self.cfg({"bodies": {"demo-town/city-council": "demo-town/planning-commission"}}))
        with self.assertRaisesRegex(ValueError, "current jurisdiction"):
            build(self.cfg({"jurisdictions": {"demo-town": None}}))

    def test_target_must_exist(self):
        with self.assertRaisesRegex(ValueError, "not in this set"):
            build(self.cfg({"bodies": {"demo-town/old": "demo-town/missing"}}))
        with self.assertRaisesRegex(ValueError, "not in this set"):
            build(self.cfg({"jurisdictions": {"old-town": "nowhere"}}))

    def test_no_chains(self):
        with self.assertRaisesRegex(ValueError, "no chains"):
            build(self.cfg({"bodies": {"demo-town/a": "demo-town/b", "demo-town/b": "demo-town/city-council"}}))
        with self.assertRaisesRegex(ValueError, "no chains"):
            build(self.cfg({"jurisdictions": {"a": "b", "b": "demo-town"}}))

    def test_body_target_must_be_a_body_feed(self):
        with self.assertRaises(ValueError):
            build(self.cfg({"bodies": {"demo-town/old": "demo-town/all"}}))


class CliExit(unittest.TestCase):
    def test_alias_failure_exits_2_and_writes_nothing(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        cfg = dict(CFG, aliases={"bodies": {"demo-town/old": "demo-town/missing"}})
        for n, d in (("in.json", fixture()), ("set.json", cfg)):
            with open(os.path.join(tmp, n), "w") as f:
                json.dump(d, f)
        argv = ["build-set", "--input", os.path.join(tmp, "in.json"), "--config", os.path.join(tmp, "set.json"),
                "--ledger", os.path.join(tmp, "l.json"), "--out", os.path.join(tmp, "out")]
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = cli.main(argv)
        self.assertEqual(rc, 2)
        self.assertIn("not in this set", err.getvalue())
        self.assertFalse(os.path.exists(os.path.join(tmp, "out")))
        self.assertFalse(os.path.exists(os.path.join(tmp, "l.json")))


class MixConstants(unittest.TestCase):
    def test_mix_is_a_copy(self):
        idx = build_feedset(fixture(), OPTS).index
        idx["mix"]["max_refs"] = 1
        self.assertEqual(MIX["max_refs"], 32)
