"""Builds the fictional demo-town feeds exactly as the README quickstart does, for the golden-file tests.

build_all() runs `meeting_ics build-set` (through cli.main) twice, v1 and then v2, with a fixed --retrieved-at and
one ledger carried from v1 to v2, and returns {relative path: bytes} for every file the real command writes:

    v1/calendar/index.json   v1/calendar/demo-town/{all,town-council,planning-commission}.ics   v1/ledger.json
    v2/...                   (same names)

Everything here is fictional: Demo Town is not a real place and the meetings do not exist.
"""
import contextlib
import io
import os
import shutil
import tempfile

from meeting_ics import cli

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "demo-town")
EXPECTED = os.path.join(FIXTURES, "expected")
OPTIONS = os.path.join(FIXTURES, "options.json")
VERSIONS = (("v1", "meetings-v1.csv", "2026-10-01T15:00:00Z"), ("v2", "meetings-v2.csv", "2026-11-20T15:00:00Z"))


def build_set(csv_name, retrieved_at, ledger, out):
    """Run build-set on one demo CSV. Returns (exit code, stdout, stderr)."""
    out_buf, err_buf = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
        code = cli.main(["build-set", "--adapter", "csv_template", "--uid-namespace", "demo-town",
                         "--retrieved-at", retrieved_at, "--input", os.path.join(FIXTURES, csv_name),
                         "--config", OPTIONS, "--ledger", ledger, "--out", out])
    return code, out_buf.getvalue(), err_buf.getvalue()


def read_tree(root, prefix, into):
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            with open(full, "rb") as f:
                into[prefix + "/" + rel] = f.read()


def build_all(tmp=None):
    """-> {"v1/calendar/index.json": bytes, ..., "v2/ledger.json": bytes}. Raises RuntimeError if a build fails."""
    own = tmp is None
    tmp = tmp or tempfile.mkdtemp(prefix="demo-town-")
    try:
        result = {}
        ledger = os.path.join(tmp, "ledger.json")  # one ledger, carried from v1 to v2 (it starts out missing)
        for version, csv_name, stamp in VERSIONS:
            out = os.path.join(tmp, version)
            code, _out, err = build_set(csv_name, stamp, ledger, out)
            if code != 0:
                raise RuntimeError("build-set %s failed (exit %d): %s" % (version, code, err))
            read_tree(out, version, result)
            with open(ledger, "rb") as f:
                result[version + "/ledger.json"] = f.read()
        return result
    finally:
        if own:
            shutil.rmtree(tmp, ignore_errors=True)
