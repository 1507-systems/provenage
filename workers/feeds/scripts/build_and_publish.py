#!/usr/bin/env python3
"""Build a feed set and publish it to an R2 bucket through wrangler.

Flow: read the previous index and ledger from the bucket, run `meeting_ics build-set` in a work directory,
check the result (hashes, ledger monotonicity, shrink guard), then upload in a fixed order:

    state/ledger.prev.json -> state/ledger.json (read back) -> changed feeds, sorted by key -> calendar/index.json
    (read back)

The ledger goes first so a published SEQUENCE is never ahead of the stored ledger. The index goes last so it never
lists an object that was not uploaded. Keys under state/ are private: the Worker never serves them.

This script reads no credentials and never prints the environment. wrangler inherits whatever the operator's
shell provides (for --target remote). --target local uses wrangler's local R2 simulator and needs none.

Exit codes: 0 ok (including "no changes"), 1 build failed, 2 usage or config error, 3 lock held,
4 shrink guard refused, 5 ledger integrity refused, 6 storage error.
"""
import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from typing import List, Optional

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
PKG = os.path.join(REPO, "packages", "meeting-ics")
FEEDS_DIR = os.path.join(REPO, "workers", "feeds")
WRANGLER = os.path.join(FEEDS_DIR, "node_modules", ".bin", "wrangler")

if PKG not in sys.path:  # the builder is a subprocess, but the ledger rules are shared code
    sys.path.insert(0, PKG)
from meeting_ics.ledger import Ledger, monotonic_problems  # noqa: E402

INDEX_SCHEMA = "provenage-calendar-index/1"
INDEX_KEY = "calendar/index.json"
LEDGER_KEY = "state/ledger.json"
LEDGER_PREV_KEY = "state/ledger.prev.json"
CT_ICS = "text/calendar; charset=utf-8"
CT_INDEX = "application/json; charset=utf-8"
CT_LEDGER = "application/json"
FEED_KEY_RE = re.compile(r"^calendar/[a-z0-9]+(-[a-z0-9]+)*/[a-z0-9]+(-[a-z0-9]+)*\.ics$")
BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
ANSI_RE = re.compile(rb"\x1b\[[0-9;?]*[A-Za-z]")
NOT_FOUND_MSG = b"The specified key does not exist"
INVALID = object()  # the previous index exists but cannot be compared

OK, E_BUILD, E_USAGE, E_LOCK, E_SHRINK, E_LEDGER, E_STORAGE = range(7)


class NotFound(Exception):
    pass


class StorageError(Exception):
    pass


class WranglerStore:
    """R2 access through the wrangler CLI. Never uses `get --file`: a missing key leaves a 0-byte file behind and
    exits non-zero, which a careless reader would take for an empty object. `--pipe` returns bytes on stdout."""

    def __init__(self, bucket, target, persist_to=None, wrangler=WRANGLER, work_dir=None):
        self.bucket, self.target, self.persist_to = bucket, target, persist_to
        self.wrangler = wrangler
        self.work_dir = work_dir or tempfile.gettempdir()

    def _flags(self):
        flags = ["--" + self.target]
        if self.persist_to:
            flags += ["--persist-to", self.persist_to]
        return flags

    def _run(self, args):
        env = dict(os.environ)
        env.setdefault("WRANGLER_SEND_METRICS", "false")
        try:
            return subprocess.run([self.wrangler] + args, cwd=FEEDS_DIR, env=env, stdin=subprocess.DEVNULL,
                                  capture_output=True, check=False)
        except OSError as e:
            raise StorageError("cannot run wrangler (%s): %s" % (self.wrangler, e)) from None

    @staticmethod
    def _tail(p):
        text = ANSI_RE.sub(b"", p.stderr + b"\n" + p.stdout).decode("utf-8", "replace")
        return " | ".join([ln.strip() for ln in text.splitlines() if ln.strip()][-5:])

    def get(self, key):
        p = self._run(["r2", "object", "get", "%s/%s" % (self.bucket, key), "--pipe"] + self._flags())
        if p.returncode == 0:
            return p.stdout
        if NOT_FOUND_MSG in ANSI_RE.sub(b"", p.stdout + p.stderr):
            raise NotFound(key)
        raise StorageError("wrangler get %s failed (exit %d): %s" % (key, p.returncode, self._tail(p)))

    def put(self, key, data, content_type):
        d = os.path.join(self.work_dir, "put")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, hashlib.sha256(data).hexdigest())
        with open(path, "wb") as f:
            f.write(data)
        p = self._run(["r2", "object", "put", "%s/%s" % (self.bucket, key), "--file", path,
                       "--content-type", content_type] + self._flags())
        if p.returncode != 0:
            raise StorageError("wrangler put %s failed (exit %d): %s" % (key, p.returncode, self._tail(p)))


def sha(data):
    return hashlib.sha256(data).hexdigest()


def run_builder(argv, env):
    """Run the builder subprocess. A function so tests can wrap it."""
    return subprocess.run(argv, env=env, stdin=subprocess.DEVNULL, capture_output=True, check=False)


def _prev_ok(prev):
    if not isinstance(prev, dict) or prev.get("schema") != INDEX_SCHEMA:
        return False
    totals, feeds = prev.get("totals"), prev.get("feeds")
    if not isinstance(totals, dict) or isinstance(totals.get("events"), bool) \
            or not isinstance(totals.get("events"), int) or not isinstance(feeds, list):
        return False
    return all(isinstance(f, dict) and isinstance(f.get("key"), str) and isinstance(f.get("sha256"), str)
               and (f.get("kind") != "combined" or (isinstance(f.get("jurisdiction"), str)
                                                    and isinstance(f.get("events"), int)))
               for f in feeds)


def shrink_problems(prev, new) -> List[str]:
    """Reasons to refuse this publish. `prev` is None (first publish), INVALID, or the parsed previous index."""
    problems = []
    n_total = new["totals"]["events"]
    if n_total == 0:
        problems.append("new feed set has 0 events")
    if prev is INVALID:
        problems.append("previous calendar/index.json is unreadable or not schema %s; cannot compare" % INDEX_SCHEMA)
        return problems
    if prev is None:
        return problems
    p_total = prev["totals"]["events"]
    if p_total > 0 and n_total * 2 < p_total:
        problems.append("total events dropped from %d to %d (more than 50%%)" % (p_total, n_total))
    new_comb = {f["jurisdiction"]: f["events"] for f in new["feeds"] if f["kind"] == "combined"}
    for f in prev["feeds"]:
        if f.get("kind") != "combined":
            continue
        j, p = f["jurisdiction"], f["events"]
        if j not in new_comb:
            problems.append("jurisdiction %s disappeared (%d events)" % (j, p))
        elif p > 0 and new_comb[j] * 2 < p:
            problems.append("jurisdiction %s events dropped from %d to %d (more than 50%%)" % (j, p, new_comb[j]))
    return problems


def _fail(code, msg):
    print(msg, file=sys.stderr)
    return code


def run(args, store, out=None) -> int:
    """The whole publish. `store` has get(key) and put(key, data, content_type). Returns the exit code."""
    out = out or sys.stdout

    def say(s):
        print(s, file=out)

    work = args.work_dir
    made_work = work is None
    if made_work:
        work = tempfile.mkdtemp(prefix="provenage-publish-")
    os.makedirs(work, exist_ok=True)
    lock_path = args.lock_file or os.path.join(
        tempfile.gettempdir(), "provenage-publish-%s-%s.lock" % (args.bucket, args.target))
    lock = open(lock_path, "a")
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return _fail(E_LOCK, "another publish is running (lock %s)" % lock_path)
        return _locked(args, store, work, say)
    finally:
        lock.close()
        if made_work:
            shutil.rmtree(work, ignore_errors=True)


def _locked(args, store, work, say) -> int:
    # 2. previous index
    try:
        prev_raw = store.get(INDEX_KEY)
    except NotFound:
        prev_raw, prev = None, None
    except StorageError as e:
        return _fail(E_STORAGE, "storage error: %s" % e)
    else:
        try:
            prev = json.loads(prev_raw.decode("utf-8"))
        except ValueError:
            prev = INVALID
        else:
            if not _prev_ok(prev):
                prev = INVALID

    # 3. old ledger. A failed read is never an empty ledger.
    try:
        old_raw = store.get(LEDGER_KEY)
    except NotFound:
        old_raw = None
    except StorageError as e:
        return _fail(E_STORAGE, "storage error: %s" % e)
    if old_raw is None:
        if prev_raw is not None and not args.init_ledger:
            return _fail(E_LEDGER, "state/ledger.json is missing but calendar/index.json exists; refusing to "
                         "restart SEQUENCE from 0 (pass --init-ledger only if you accept that subscribers may "
                         "miss updates)")
        if args.init_ledger:
            say("WARNING: initializing an empty ledger")
        old = Ledger()
    else:
        if args.init_ledger:
            return _fail(E_USAGE, "--init-ledger only applies when state/ledger.json is missing")
        try:
            old = Ledger.loads(old_raw)
        except ValueError as e:
            return _fail(E_LEDGER, str(e))
    old_sha = sha(old_raw) if old_raw is not None else None

    # 4. build
    out_dir = os.path.join(work, "out")
    ledger_path = os.path.join(work, "ledger.json")
    shutil.rmtree(out_dir, ignore_errors=True)
    with open(ledger_path, "wb") as f:
        f.write(old_raw if old_raw is not None else b"{}\n")
    cmd = [sys.executable, "-m", "meeting_ics", "build-set", "--input", args.input, "--config", args.config,
           "--ledger", ledger_path, "--out", out_dir]
    if args.adapter:
        cmd += ["--adapter", args.adapter]
    if args.uid_namespace:
        cmd += ["--uid-namespace", args.uid_namespace]
    if args.retrieved_at:
        cmd += ["--retrieved-at", args.retrieved_at]
    env = dict(os.environ)
    env["PYTHONPATH"] = PKG + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    p = run_builder(cmd, env)
    if p.stderr:
        sys.stderr.write(p.stderr.decode("utf-8", "replace"))
    if p.returncode != 0:
        return _fail(E_BUILD, "build failed (builder exit %d); nothing published" % p.returncode)
    if p.stdout:
        say(p.stdout.decode("utf-8", "replace").rstrip())

    # 5. load the result
    try:
        with open(os.path.join(out_dir, INDEX_KEY), "rb") as f:
            index_bytes = f.read()
        index = json.loads(index_bytes.decode("utf-8"))
        with open(ledger_path, "rb") as f:
            new_ledger_bytes = f.read()
        new = Ledger.loads(new_ledger_bytes)
        feed_bytes = {}
        for ent in index["feeds"]:
            key = ent["key"]
            if not FEED_KEY_RE.match(key):
                return _fail(E_BUILD, "built index has an unexpected key %r" % key)
            with open(os.path.join(out_dir, key), "rb") as f:
                feed_bytes[key] = f.read()
            if sha(feed_bytes[key]) != ent["sha256"]:
                return _fail(E_BUILD, "built file %s does not match index sha256" % key)
    except (OSError, ValueError, KeyError, TypeError) as e:
        return _fail(E_BUILD, "cannot read the build output: %s" % e)

    # 6. ledger monotonicity
    problems = monotonic_problems(old, new)
    if problems:
        return _fail(E_LEDGER, "\n".join("refused: " + x for x in problems))

    # 7. shrink guard
    problems = shrink_problems(prev, index)
    if problems:
        if not args.force:
            return _fail(E_SHRINK, "\n".join("refused: " + x for x in problems))
        for x in problems:
            say("WARNING (forced): " + x)

    # 8. plan: (key, data, content type, label)
    plan = []
    if old_raw is not None and new_ledger_bytes != old_raw:
        plan.append((LEDGER_PREV_KEY, old_raw, CT_LEDGER, "backup"))
    if new_ledger_bytes != old_raw:
        plan.append((LEDGER_KEY, new_ledger_bytes, CT_LEDGER, "changed" if old_raw is not None else "new"))
    prev_feeds = {f["key"]: f["sha256"] for f in prev["feeds"]} if isinstance(prev, dict) else {}
    for ent in sorted(index["feeds"], key=lambda e: e["key"]):
        k = ent["key"]
        if k not in prev_feeds:
            plan.append((k, feed_bytes[k], CT_ICS, "new"))
        elif prev_feeds[k] != ent["sha256"]:
            plan.append((k, feed_bytes[k], CT_ICS, "changed"))
        else:
            say("plan: SKIP %s (unchanged)" % k)
    if prev_raw is None or index_bytes != prev_raw:
        plan.append((INDEX_KEY, index_bytes, CT_INDEX, "new" if prev_raw is None else "changed"))
    else:
        say("plan: SKIP %s (unchanged)" % INDEX_KEY)
    new_keys = {e["key"] for e in index["feeds"]}
    for k in sorted(prev_feeds):
        if k not in new_keys:
            say("plan: ORPHAN %s (in previous index, not in new; left in place)" % k)
    for key, data, _ct, label in plan:
        say("plan: PUT %s (%s) sha256=%s" % (key, label, sha(data)[:12]))
    if not plan:
        say("no changes")
        _public_urls(args, index, say)
        return OK

    # 9. dry run
    if args.dry_run:
        say("dry-run: nothing written")
        return OK

    # 10. pre-write check: the ledger must be what this run was built from
    try:
        again = store.get(LEDGER_KEY)
    except NotFound:
        again = None
    except StorageError as e:
        return _fail(E_STORAGE, "storage error: %s" % e)
    if (sha(again) if again is not None else None) != old_sha:
        return _fail(E_LEDGER, "state/ledger.json changed during this run (another publisher?); nothing written")

    # 11. upload in plan order
    feed_plan = [x for x in plan if x[0] not in (LEDGER_KEY, LEDGER_PREV_KEY, INDEX_KEY)]
    done = 0
    try:
        for key, data, ct, _label in plan:
            try:
                store.put(key, data, ct)
            except StorageError as e:
                if key in (LEDGER_KEY, LEDGER_PREV_KEY, INDEX_KEY):
                    raise
                return _fail(E_STORAGE, "storage error: %s\npublish incomplete: %d of %d feeds uploaded; "
                             "calendar/index.json not updated; rerun to finish" % (e, done, len(feed_plan)))
            if key in (LEDGER_KEY, INDEX_KEY):
                if store.get(key) != data:
                    return _fail(E_STORAGE, "readback of %s does not match what was written" % key)
            elif key not in (LEDGER_PREV_KEY,):
                done += 1
    except (StorageError, NotFound) as e:
        return _fail(E_STORAGE, "storage error: %s" % e)
    say("published %d object(s)" % len(plan))
    _public_urls(args, index, say)
    return OK


def _public_urls(args, index, say):
    if args.public_base:
        for ent in index["feeds"]:
            say("Public URL: %s/%s" % (args.public_base, ent["key"]))


def parse_args(argv):
    ap = argparse.ArgumentParser(prog="build_and_publish.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("--input", required=True, help="records file (JSON, or the adapter's source file)")
    ap.add_argument("--config", required=True, help="build-set config JSON (uid_domain, jurisdictions, ...)")
    ap.add_argument("--bucket", required=True, help="R2 bucket name")
    ap.add_argument("--target", required=True, choices=["local", "remote"],
                    help="local = wrangler's local simulator; remote = the real bucket (needs the operator's env)")
    ap.add_argument("--persist-to", help="local simulator state directory (only with --target local)")
    ap.add_argument("--dry-run", action="store_true", help="build, check and print the plan; write nothing")
    ap.add_argument("--force", action="store_true", help="publish despite a shrink-guard refusal")
    ap.add_argument("--init-ledger", action="store_true",
                    help="start an empty ledger although calendar/index.json exists (resets SEQUENCE)")
    ap.add_argument("--work-dir", help="scratch directory (default: a temporary directory removed afterwards)")
    ap.add_argument("--lock-file", help="lock file path (default: in the temp directory, per bucket and target)")
    ap.add_argument("--public-base", help="print Public URL: <base>/<key> for every feed after success")
    ap.add_argument("--adapter", help="passed to build-set")
    ap.add_argument("--uid-namespace", help="passed to build-set")
    ap.add_argument("--retrieved-at", help="passed to build-set (csv_template adapter only)")
    return ap.parse_args(argv)


def check_args(a) -> Optional[str]:
    if a.persist_to and a.target != "local":
        return "--persist-to only applies to --target local"
    if not BUCKET_RE.match(a.bucket):
        return "--bucket %r is not a valid R2 bucket name" % a.bucket
    for flag, path in (("--input", a.input), ("--config", a.config)):
        if not os.path.isfile(path):
            return "%s file not found: %s" % (flag, path)
    try:
        with open(a.config, encoding="utf-8") as f:
            if not isinstance(json.load(f), dict):
                return "--config must be a JSON object"
    except (OSError, ValueError) as e:
        return "--config is not readable JSON: %s" % e
    if a.public_base:
        a.public_base = a.public_base.rstrip("/")
        if not re.match(r"^https://[^\s/?#]+(/[^\s?#]*)?$", a.public_base):
            return "--public-base must be an https URL without query or fragment"
    return None


def main(argv=None) -> int:
    a = parse_args(argv)
    err = check_args(a)
    if err:
        return _fail(E_USAGE, "error: " + err)
    if not os.path.isfile(WRANGLER):
        return _fail(E_USAGE, "error: wrangler not found at %s; run `npm ci` in workers/feeds" % WRANGLER)
    work = a.work_dir
    if work is None:
        work = a.work_dir = tempfile.mkdtemp(prefix="provenage-publish-")
        made = True
    else:
        made = False
    try:
        store = WranglerStore(a.bucket, a.target, a.persist_to, work_dir=work)
        return run(a, store)
    finally:
        if made:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
