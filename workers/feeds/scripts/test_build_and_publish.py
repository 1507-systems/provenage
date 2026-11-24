"""Tests for build_and_publish.py.

P*: in-process runs against a dict-backed MemoryStore (the real builder runs as a subprocess).
W*: class WranglerLocal runs against wrangler's local R2 simulator. It is skipped unless PROVENAGE_WRANGLER_LOCAL=1
and the wrangler binary exists. No test here touches a remote bucket or any credential.

Works under pytest and under `python3 -m unittest discover -s workers/feeds/scripts`.
"""
import argparse
import contextlib
import fcntl
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "..", "..", "packages", "meeting-ics"))

import build_and_publish as bp  # noqa: E402
from meeting_ics.ledger import Ledger  # noqa: E402

LEDGER, PREV, INDEX = bp.LEDGER_KEY, bp.LEDGER_PREV_KEY, bp.INDEX_KEY
F_ALL = "calendar/demo-town/all.ics"
F_CC = "calendar/demo-town/city-council.ics"
F_PC = "calendar/demo-town/planning-commission.ics"
CONFIG = {"uid_domain": "example.org", "jurisdictions": {"demo-town": {"name": "Demo Town"}}}


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
             status="cancelled"),
    ]


class MemoryStore:
    """Dict-backed store with fault hooks. `puts` records the order of writes."""

    def __init__(self):
        self.data = {}
        self.puts = []
        self.fail_keys = set()
        self.corrupt_keys = set()
        self.get_hooks = {}  # key -> callable(store, n) called before the n-th get of that key (1-based)
        self.get_fail = set()
        self.gets = {}

    def get(self, key):
        n = self.gets[key] = self.gets.get(key, 0) + 1
        if key in self.get_fail:
            raise bp.StorageError("injected get failure for %s" % key)
        if key in self.get_hooks:
            self.get_hooks[key](self, n)
        if key not in self.data:
            raise bp.NotFound(key)
        return self.data[key]

    def put(self, key, data, content_type):
        if key in self.fail_keys:
            raise bp.StorageError("injected put failure for %s" % key)
        self.puts.append(key)
        self.data[key] = data + b"#corrupt" if key in self.corrupt_keys else data


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bp-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.config = os.path.join(self.tmp, "config.json")
        with open(self.config, "w") as f:
            json.dump(CONFIG, f)
        self.store = MemoryStore()
        self.n = 0

    def write_input(self, records):
        path = os.path.join(self.tmp, "records.json")
        with open(path, "w") as f:
            json.dump(records, f)
        return path

    def args(self, records=None, **kw):
        d = dict(input=self.write_input(records if records is not None else fixture()), config=self.config,
                 bucket="test-bucket", target="local", persist_to=None, dry_run=False, force=False,
                 init_ledger=False, work_dir=os.path.join(self.tmp, "work"),
                 lock_file=os.path.join(self.tmp, "lock"), public_base=None, adapter=None, uid_namespace=None,
                 retrieved_at=None)
        d.update(kw)
        return argparse.Namespace(**d)

    def go(self, args=None, store=None):
        """Run and return (exit code, stdout, stderr). Clears the put log first."""
        store = store or self.store
        if hasattr(store, "puts"):
            store.puts.clear()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = bp.run(args or self.args(), store, out)
        return code, out.getvalue(), err.getvalue()

    def index(self):
        return json.loads(self.store.data[INDEX])


class Publish(Base):
    def test_p1_first_publish_order(self):
        code, out, _ = self.go()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.store.puts, [LEDGER, F_ALL, F_CC, F_PC, INDEX])
        self.assertEqual(self.index()["schema"], bp.INDEX_SCHEMA)
        for ent in self.index()["feeds"]:
            self.assertEqual(bp.sha(self.store.data[ent["key"]]), ent["sha256"])
        self.assertIn("plan: PUT calendar/index.json (new) sha256=", out)

    def test_p2_rerun_is_a_no_op(self):
        self.go()
        code, out, _ = self.go()
        self.assertEqual(code, 0)
        self.assertEqual(self.store.puts, [])
        self.assertIn("no changes", out)

    def test_p3_change_puts_backup_then_ledger_then_changed_feeds(self):
        self.go()
        first_ledger = self.store.data[LEDGER]
        recs = fixture()
        recs[0]["start_time"] = "18:30"
        recs[0]["retrieved_at"] = "2026-03-02T06:00:00Z"
        code, out, _ = self.go(self.args(recs))
        self.assertEqual(code, 0, out)
        self.assertEqual(self.store.puts, [PREV, LEDGER, F_ALL, F_CC, INDEX])
        self.assertEqual(self.store.data[PREV], first_ledger)
        self.assertIn("plan: SKIP %s (unchanged)" % F_PC, out)
        self.assertIn(b"SEQUENCE:1", self.store.data[F_CC])

    def test_p4_dry_run_writes_nothing(self):
        self.go()
        recs = fixture()
        recs[0]["start_time"] = "18:30"
        recs[0]["retrieved_at"] = "2026-03-02T06:00:00Z"
        before = dict(self.store.data)
        code, out, _ = self.go(self.args(recs, dry_run=True))
        self.assertEqual(code, 0)
        self.assertEqual(self.store.puts, [])
        self.assertEqual(self.store.data, before)
        self.assertIn("plan: PUT %s (changed)" % F_CC, out)
        self.assertIn("dry-run: nothing written", out)

    def test_dry_run_on_empty_store_writes_nothing(self):
        code, out, _ = self.go(self.args(dry_run=True))
        self.assertEqual(code, 0)
        self.assertEqual(self.store.data, {})
        self.assertIn("dry-run: nothing written", out)

    def test_public_base_prints_urls(self):
        code, out, _ = self.go(self.args(public_base="https://api.example.org/provenage"))
        self.assertEqual(code, 0)
        self.assertIn("Public URL: https://api.example.org/provenage/%s" % F_ALL, out)
        self.assertNotIn("Public URL: https://api.example.org/provenage/state", out)

    def test_orphan_is_reported_and_left(self):
        self.go()
        recs = [r for r in fixture() if r["body_slug"] != "planning-commission"]
        code, out, _ = self.go(self.args(recs))
        self.assertEqual(code, 0, out)  # 2 of 3 events: exactly above the 50 percent line
        self.assertIn("plan: ORPHAN %s (in previous index, not in new; left in place)" % F_PC, out)
        self.assertIn(F_PC, self.store.data)

    def test_ledger_never_under_calendar_prefix(self):
        self.go()
        self.assertTrue(all(k.startswith(("calendar/", "state/")) for k in self.store.data))
        self.assertEqual([k for k in self.store.data if k.startswith("state/")], [LEDGER])


class ShrinkGuard(Base):
    def seeded(self, n_cc, n_pc=0, j="demo-town"):
        recs = [base("C%d" % i, "City Council", "city-council", "2026-04-%02d" % (i + 1), "18:00", "Meeting", j=j)
                for i in range(n_cc)]
        recs += [base("P%d" % i, "Planning", "planning", "2026-05-%02d" % (i + 1), "18:00", "Meeting", j=j)
                 for i in range(n_pc)]
        return recs

    def test_p5_halving_rule(self):
        self.assertEqual(self.go(self.args(self.seeded(10)))[0], 0)
        before = dict(self.store.data)
        code, _, err = self.go(self.args(self.seeded(4)))
        self.assertEqual(code, 4)
        self.assertIn("refused: total events dropped from 10 to 4 (more than 50%)", err)
        self.assertEqual(self.store.puts, [])
        self.assertEqual(self.store.data, before)
        self.assertEqual(self.go(self.args(self.seeded(5)))[0], 0)  # exactly 50% passes

    def test_p5_force(self):
        self.go(self.args(self.seeded(10)))
        code, out, _ = self.go(self.args(self.seeded(4), force=True))
        self.assertEqual(code, 0)
        self.assertIn("WARNING (forced): total events dropped from 10 to 4 (more than 50%)", out)

    def test_zero_events_refused_even_when_first_publish(self):
        # The builder rejects an empty input, so stub its output to reach the guard.
        real = bp.run_builder

        def zero(cmd, env):
            p = real(cmd, env)
            out_dir = cmd[cmd.index("--out") + 1]
            idx_path = os.path.join(out_dir, INDEX)
            idx = json.load(open(idx_path))
            for f in idx["feeds"]:
                f["events"] = 0
            idx["totals"]["events"] = 0
            data = bp.json.dumps(idx, sort_keys=True, indent=1).encode() + b"\n"
            open(idx_path, "wb").write(data)
            return p

        with mock.patch.object(bp, "run_builder", zero):
            code, _, err = self.go()
        self.assertEqual(code, 4)
        self.assertIn("refused: new feed set has 0 events", err)
        self.assertEqual(self.store.puts, [])

    def test_p6_jurisdiction_shrink_and_disappearance(self):
        both = self.seeded(10) + self.seeded(10, j="other-city")
        for r in both[10:]:
            r["record_id"] = "O" + r["record_id"]
        self.assertEqual(self.go(self.args(both))[0], 0)
        shrunk = both[:10] + both[10:13]
        code, _, err = self.go(self.args(shrunk))
        self.assertEqual(code, 4)
        self.assertIn("refused: jurisdiction other-city events dropped from 10 to 3 (more than 50%)", err)
        gone = both[:10] + both[10:]  # keep totals high, drop one jurisdiction's feed entirely instead
        gone = [r for r in gone if r["jurisdiction_slug"] == "demo-town"] + \
               [dict(r, record_id="X" + r["record_id"]) for r in both[:10]]
        code, _, err = self.go(self.args(gone))
        self.assertEqual(code, 4)
        self.assertIn("jurisdiction other-city disappeared (10 events)", err)

    def test_p13_unreadable_previous_index(self):
        self.store.data[INDEX] = b'{"feeds":[]}'
        self.store.data[LEDGER] = b"{}\n"
        code, _, err = self.go()
        self.assertEqual(code, 4)
        self.assertIn("cannot compare", err)
        self.assertEqual(self.go(self.args(force=True))[0], 0)

    def test_p13_garbage_previous_index(self):
        self.store.data[INDEX] = b"\xff not json"
        self.store.data[LEDGER] = b"{}\n"
        self.assertEqual(self.go()[0], 4)


class LedgerIntegrity(Base):
    def test_p7_missing_ledger_with_index(self):
        self.go()
        del self.store.data[LEDGER]
        code, _, err = self.go()
        self.assertEqual(code, 5)
        self.assertIn("refusing to restart SEQUENCE from 0", err)
        self.assertEqual(self.store.puts, [])
        code, out, _ = self.go(self.args(init_ledger=True))
        self.assertEqual(code, 0)
        self.assertIn("WARNING: initializing an empty ledger", out)

    def test_p7_init_ledger_with_ledger_present_is_usage_error(self):
        self.go()
        code, _, err = self.go(self.args(init_ledger=True))
        self.assertEqual(code, 2)
        self.assertIn("--init-ledger only applies", err)
        self.assertEqual(self.store.puts, [])

    def test_malformed_ledger_is_never_an_empty_one(self):
        for bad in (b"", b"not json", b"[]", b'{"u":{"sequence":1}}'):
            store = MemoryStore()
            store.data[LEDGER] = bad
            code, _, err = self.go(store=store)
            self.assertEqual(code, 5, bad)
            self.assertIn("malformed ledger", err)
            self.assertEqual(store.puts, [])

    def test_p8_ledger_read_failure(self):
        self.store.get_fail.add(LEDGER)
        code, _, err = self.go()
        self.assertEqual(code, 6)
        self.assertIn("injected get failure", err)
        self.assertEqual(self.store.puts, [])

    def test_index_read_failure(self):
        self.store.get_fail.add(INDEX)
        self.assertEqual(self.go()[0], 6)
        self.assertEqual(self.store.puts, [])

    def test_p9_ledger_changed_during_run(self):
        self.go()
        recs = fixture()
        recs[0]["start_time"] = "18:30"
        recs[0]["retrieved_at"] = "2026-03-02T06:00:00Z"

        def tamper(store, n):
            if n == 2:
                led = json.loads(store.data[LEDGER])
                next(iter(led.values()))["sequence"] += 5
                store.data[LEDGER] = json.dumps(led, indent=1, sort_keys=True).encode() + b"\n"

        self.store.gets.clear()
        self.store.get_hooks[LEDGER] = tamper
        code, _, err = self.go(self.args(recs))
        self.assertEqual(code, 5)
        self.assertIn("changed during this run", err)
        self.assertEqual(self.store.puts, [])

    def test_p11_builder_that_drops_a_uid(self):
        self.go()
        recs = fixture()
        recs[0]["start_time"] = "18:30"
        recs[0]["retrieved_at"] = "2026-03-02T06:00:00Z"
        real = bp.run_builder

        def drop(cmd, env):
            p = real(cmd, env)
            path = cmd[cmd.index("--ledger") + 1]
            led = json.load(open(path))
            led.pop(sorted(led)[0])
            json.dump(led, open(path, "w"), indent=1, sort_keys=True)
            return p

        with mock.patch.object(bp, "run_builder", drop):
            code, _, err = self.go(self.args(recs))
        self.assertEqual(code, 5)
        self.assertIn("missing from new ledger", err)
        self.assertEqual(self.store.puts, [])

    def test_decreased_sequence_refused(self):
        self.go()
        recs = fixture()
        recs[0]["start_time"] = "18:30"
        recs[0]["retrieved_at"] = "2026-03-02T06:00:00Z"
        real = bp.run_builder

        def lower(cmd, env):
            p = real(cmd, env)
            path = cmd[cmd.index("--ledger") + 1]
            led = json.load(open(path))
            for e in led.values():
                e["sequence"] = 0
            json.dump(led, open(path, "w"), indent=1, sort_keys=True)
            return p

        # Old ledger needs a non-zero sequence for a decrease to be visible.
        led = json.loads(self.store.data[LEDGER])
        for e in led.values():
            e["sequence"] = 3
        self.store.data[LEDGER] = json.dumps(led, indent=1, sort_keys=True).encode() + b"\n"
        with mock.patch.object(bp, "run_builder", lower):
            code, _, err = self.go(self.args(recs))
        self.assertEqual(code, 5)
        self.assertIn("sequence decreased 3 -> 0", err)

    def test_p10_partial_upload_then_rerun_completes(self):
        clean = MemoryStore()
        self.go(store=clean)
        self.store.fail_keys.add(F_CC)
        code, _, err = self.go()
        self.assertEqual(code, 6)
        self.assertIn("publish incomplete: 1 of 3 feeds uploaded; calendar/index.json not updated; rerun to finish",
                      err)
        self.assertNotIn(INDEX, self.store.data)
        self.assertIn(LEDGER, self.store.data)
        self.store.fail_keys.clear()
        # A later rerun still sees "no index" so the ledger is treated as present and unchanged.
        code, _, _ = self.go()
        self.assertEqual(code, 0)
        self.assertNotIn(LEDGER, self.store.puts)
        self.assertEqual(self.store.data, clean.data)

    def test_p14_ledger_readback_mismatch(self):
        self.store.corrupt_keys.add(LEDGER)
        code, _, err = self.go()
        self.assertEqual(code, 6)
        self.assertIn("readback of state/ledger.json does not match what was written", err)
        self.assertEqual(self.store.puts, [LEDGER])

    def test_index_readback_mismatch(self):
        self.store.corrupt_keys.add(INDEX)
        code, _, err = self.go()
        self.assertEqual(code, 6)
        self.assertIn("readback of calendar/index.json does not match", err)

    def test_ledger_put_failure_stops_before_feeds(self):
        self.store.fail_keys.add(LEDGER)
        code, _, _ = self.go()
        self.assertEqual(code, 6)
        self.assertEqual(self.store.puts, [])


class Exits(Base):
    def test_p12_lock_held(self):
        lock_path = os.path.join(self.tmp, "lock")
        with open(lock_path, "a") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            code, _, err = self.go()
        self.assertEqual(code, 3)
        self.assertIn("another publish is running", err)
        self.assertEqual(self.store.puts, [])
        self.assertEqual(self.go()[0], 0)  # released

    def test_build_failure_exit_1(self):
        recs = fixture()
        recs[0]["status"] = "bogus"
        code, _, err = self.go(self.args(recs))
        self.assertEqual(code, 1)
        self.assertIn("build failed", err)
        self.assertEqual(self.store.puts, [])

    def test_empty_input_is_a_build_failure(self):
        code, _, _ = self.go(self.args([]))
        self.assertEqual(code, 1)

    def test_built_file_hash_mismatch(self):
        real = bp.run_builder

        def tamper(cmd, env):
            p = real(cmd, env)
            path = os.path.join(cmd[cmd.index("--out") + 1], F_CC)
            with open(path, "ab") as f:
                f.write(b"X")
            return p

        with mock.patch.object(bp, "run_builder", tamper):
            code, _, err = self.go()
        self.assertEqual(code, 1)
        self.assertIn("does not match index sha256", err)

    def test_pass_through_flags(self):
        seen = {}
        real = bp.run_builder

        def spy(cmd, env):
            seen["cmd"] = cmd
            return real(cmd, env)

        with mock.patch.object(bp, "run_builder", spy):
            self.go(self.args(adapter="nope", uid_namespace="ns", retrieved_at="2026-01-01T00:00:00Z"))
        cmd = seen["cmd"]
        for flag, val in (("--adapter", "nope"), ("--uid-namespace", "ns"), ("--retrieved-at", "2026-01-01T00:00:00Z")):
            self.assertEqual(cmd[cmd.index(flag) + 1], val)

    def test_csv_adapter_end_to_end(self):
        repo = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
        header = open(os.path.join(repo, "packages", "meeting-ics", "meeting_ics", "adapters",
                                   "csv_template.csv"), encoding="utf-8").read().splitlines()[0]
        cols = header.split(",")
        row = {"record_id": "1", "jurisdiction_slug": "demo-town", "body_slug": "board", "body_name": "Board",
               "meeting_type": "Regular Meeting", "date": "2026-06-01", "start_time": "18:00",
               "timezone": "America/Denver", "status": "scheduled"}
        path = os.path.join(self.tmp, "m.csv")
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(header + "\n" + ",".join(row.get(c, "") for c in cols) + "\n")
        a = self.args(adapter="csv_template", uid_namespace="demo", retrieved_at="2026-05-01T00:00:00Z")
        a.input = path
        code, out, err = self.go(a)
        self.assertEqual(code, 0, err)
        self.assertIn("calendar/demo-town/board.ics", self.store.data)

    def test_check_args(self):
        a = self.args()
        self.assertIsNone(bp.check_args(a))
        for kw, text in ((dict(target="remote", persist_to="x"), "--persist-to only applies to --target local"),
                         (dict(bucket="Bad_Bucket"), "valid R2 bucket name"),
                         (dict(input="/nonexistent/records.json"), "--input file not found"),
                         (dict(public_base="http://insecure.example"), "https URL")):
            msg = bp.check_args(self.args(**kw))
            self.assertIn(text, msg or "")

    def test_main_usage_errors_exit_2(self):
        with contextlib.redirect_stderr(io.StringIO()):
            code = bp.main(["--input", "/nonexistent", "--config", self.config, "--bucket", "test-bucket",
                            "--target", "local"])
        self.assertEqual(code, 2)

    def test_target_is_required(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            bp.parse_args(["--input", "a", "--config", "b", "--bucket", "test-bucket"])
        self.assertEqual(cm.exception.code, 2)


class Units(unittest.TestCase):
    def test_wrangler_store_get_maps_not_found_and_errors(self):
        st = bp.WranglerStore("b", "local", wrangler="/bin/true")
        ok = mock.Mock(returncode=0, stdout=b"data", stderr=b"")
        missing = mock.Mock(returncode=1, stdout=b"", stderr=b"\x1b[31mThe specified key does not exist.\x1b[0m")
        other = mock.Mock(returncode=1, stdout=b"", stderr=b"boom")
        with mock.patch.object(st, "_run", side_effect=[ok, missing, other]) as run:
            self.assertEqual(st.get("k"), b"data")
            self.assertIn("--pipe", run.call_args_list[0][0][0])
            self.assertNotIn("--file", run.call_args_list[0][0][0])
            with self.assertRaises(bp.NotFound):
                st.get("k")
            with self.assertRaises(bp.StorageError):
                st.get("k")

    def test_shrink_problems_exactly_half_passes(self):
        idx = lambda n: {"totals": {"events": n}, "feeds": []}  # noqa: E731
        prev = {"schema": bp.INDEX_SCHEMA, "totals": {"events": 10}, "feeds": []}
        self.assertEqual(bp.shrink_problems(prev, idx(5)), [])
        self.assertEqual(len(bp.shrink_problems(prev, idx(4))), 1)

    def test_no_credentials_referenced(self):
        src = open(bp.__file__, encoding="utf-8").read()
        code = src.split('"""', 2)[2]  # everything after the module docstring
        for word in ("API_TOKEN", "wicket", "keychain", "Authorization"):
            self.assertNotIn(word, code)


WRANGLER_OK = os.environ.get("PROVENAGE_WRANGLER_LOCAL") == "1" and os.path.isfile(bp.WRANGLER)


@unittest.skipUnless(WRANGLER_OK, "set PROVENAGE_WRANGLER_LOCAL=1 and run `npm ci` in workers/feeds")
class WranglerLocal(Base):
    def setUp(self):
        super().setUp()
        self.persist = os.path.join(self.tmp, "r2-state")
        self.ws = bp.WranglerStore("provenage-test", "local", self.persist, work_dir=self.tmp)

    def test_w1_missing_key_is_not_found(self):
        with self.assertRaises(bp.NotFound):
            self.ws.get("state/nope.json")

    def test_w2_bytes_round_trip(self):
        data = b"A\r\nB\xc3\xa9\r\n"
        self.ws.put("calendar/x/y.ics", data, bp.CT_ICS)
        self.assertEqual(self.ws.get("calendar/x/y.ics"), data)

    def test_w3_full_run_and_rerun(self):
        a = self.args(persist_to=self.persist)
        code, out, err = self.go(a, store=self.ws)
        self.assertEqual(code, 0, err)
        idx = json.loads(self.ws.get(INDEX))
        self.assertEqual(idx["schema"], bp.INDEX_SCHEMA)
        for ent in idx["feeds"]:
            self.assertEqual(bp.sha(self.ws.get(ent["key"])), ent["sha256"])
        Ledger.loads(self.ws.get(LEDGER))
        puts = []
        real = self.ws.put
        self.ws.put = lambda k, d, c: (puts.append(k), real(k, d, c))[1]
        code, out, _ = self.go(self.args(persist_to=self.persist), store=self.ws)
        self.assertEqual(code, 0)
        self.assertEqual(puts, [])
        self.assertIn("no changes", out)

    def test_main_end_to_end_local(self):
        code = bp.main(["--input", self.write_input(fixture()), "--config", self.config, "--bucket", "provenage-test",
                        "--target", "local", "--persist-to", self.persist, "--lock-file",
                        os.path.join(self.tmp, "lock2"), "--public-base", "https://example.org/p"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(self.ws.get(INDEX))["totals"]["events"], 3)


if __name__ == "__main__":
    unittest.main()
