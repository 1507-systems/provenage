"""Ledger strictness, atomic save and monotonicity (C3)."""
import json
import os
import tempfile
import unittest
from unittest import mock

from meeting_ics import Ledger
from meeting_ics.ledger import atomic_write, monotonic_problems

ENTRY = {"critical_hash": "a" * 64, "docs_hash": "b" * 64, "sequence": 2, "last_modified": "2026-01-01T00:00:00Z"}


def entry(**kw):
    e = dict(ENTRY)
    e.update(kw)
    return e


class Loads(unittest.TestCase):
    def test_round_trip_bytes(self):
        led = Ledger({"u2": entry(), "u1": entry(sequence=0)})
        data = led.dumps()
        self.assertEqual(data, (json.dumps(led.entries, indent=1, sort_keys=True) + "\n").encode())
        self.assertEqual(Ledger.loads(data).entries, led.entries)

    def test_malformed(self):
        cases = [
            (b"{", "malformed ledger: <root>: not valid JSON ("),
            (b"", "malformed ledger: <root>: not valid JSON ("),
            (b"\xff", "malformed ledger: <root>: not valid UTF-8"),
            (b"[]", "malformed ledger: <root>: top level must be a JSON object"),
            (json.dumps({"u": 5}).encode(), "malformed ledger: u: entry must be a JSON object"),
            (json.dumps({"u": entry(sequence=-1)}).encode(), "malformed ledger: u: sequence must be"),
            (json.dumps({"u": entry(sequence=True)}).encode(), "malformed ledger: u: sequence must be"),
            (json.dumps({"u": entry(sequence="1")}).encode(), "malformed ledger: u: sequence must be"),
            (json.dumps({"u": entry(critical_hash=1)}).encode(), "malformed ledger: u: critical_hash must be"),
            (json.dumps({"u": entry(last_modified="yesterday")}).encode(), "malformed ledger: u: last_modified"),
            (json.dumps({"u": {"sequence": 0}}).encode(), "malformed ledger: u: missing key"),
        ]
        for data, prefix in cases:
            with self.subTest(data=data):
                with self.assertRaises(ValueError) as cm:
                    Ledger.loads(data)
                self.assertTrue(str(cm.exception).startswith(prefix), str(cm.exception))
        with self.assertRaisesRegex(ValueError, "refusing to start a fresh ledger because that would reset SEQUENCE"):
            Ledger.loads(b"{")


class Files(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.dir = self._td.name

    def test_missing_file_is_fresh(self):
        self.assertEqual(Ledger.load(os.path.join(self.dir, "none.json")).entries, {})

    def test_empty_file_is_malformed(self):
        p = os.path.join(self.dir, "l.json")
        open(p, "wb").close()
        with self.assertRaisesRegex(ValueError, "^malformed ledger"):
            Ledger.load(p)

    def test_save_is_atomic_and_leaves_no_tmp(self):
        p = os.path.join(self.dir, "sub", "l.json")
        Ledger({"u": entry()}).save(p)
        self.assertEqual(os.listdir(os.path.dirname(p)), ["l.json"])
        self.assertEqual(Ledger.load(p).entries, {"u": entry()})

    def test_failed_save_keeps_old_file_and_removes_tmp(self):
        p = os.path.join(self.dir, "l.json")
        Ledger({"u": entry()}).save(p)
        bad = Ledger({"u": object()})
        with self.assertRaises(TypeError):
            bad.save(p)
        self.assertEqual(os.listdir(self.dir), ["l.json"])
        self.assertEqual(Ledger.load(p).entries, {"u": entry()})


class AtomicWriteSafety(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.dir = self._td.name

    def test_does_not_follow_symlink_at_old_tmp_name(self):
        target = os.path.join(self.dir, "victim.txt")
        with open(target, "w") as f:
            f.write("keep")
        out = os.path.join(self.dir, "out.ics")
        try:
            os.symlink(target, out + ".tmp")
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        atomic_write(out, b"feed")
        with open(target) as f:
            self.assertEqual(f.read(), "keep")
        self.assertFalse(os.path.islink(out))
        with open(out, "rb") as f:
            self.assertEqual(f.read(), b"feed")

    def test_unique_temp_name_and_cleanup_on_failure(self):
        out = os.path.join(self.dir, "out.ics")
        with mock.patch("os.replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                atomic_write(out, b"x")
        self.assertEqual(os.listdir(self.dir), [])
        atomic_write(out, b"x")
        self.assertEqual(os.listdir(self.dir), ["out.ics"])

    def test_output_is_world_readable(self):
        out = os.path.join(self.dir, "out.ics")
        atomic_write(out, b"x")
        self.assertEqual(os.stat(out).st_mode & 0o777, 0o644)

    def test_output_honours_umask(self):
        out = os.path.join(self.dir, "out.ics")
        old = os.umask(0o077)
        try:
            atomic_write(out, b"x")
        finally:
            os.umask(old)
        self.assertEqual(os.stat(out).st_mode & 0o777, 0o600)
        old = os.umask(0o002)
        try:
            atomic_write(out, b"y")  # replaces the earlier file; its old mode is not kept
        finally:
            os.umask(old)
        self.assertEqual(os.stat(out).st_mode & 0o777, 0o664)


class Monotonic(unittest.TestCase):
    def test_problems(self):
        old = Ledger({"a": entry(sequence=3), "b": entry(sequence=1), "c": entry(sequence=0)})
        new = Ledger({"a": entry(sequence=2), "b": entry(sequence=1), "d": entry(sequence=0)})
        self.assertEqual(monotonic_problems(old, new),
                         ["uid a sequence decreased 3 -> 2", "uid c missing from new ledger"])
        self.assertEqual(monotonic_problems(old, old), [])
        self.assertEqual(monotonic_problems(old.entries, {**old.entries, "z": entry()}), [])


if __name__ == "__main__":
    unittest.main()
