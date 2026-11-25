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
4 publisher guard refused (shrink, alias, vanished feed),
5 ledger integrity refused, 6 storage or file-system error, 7 unexpected internal error.
"""
import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.parse
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
# A get is "not found" only when the output positively names a missing KEY (R2 NoSuchKey, API code 10007).
# Anything else, including a missing bucket (10006) or an auth failure, is a storage error: fail safe.
NOT_FOUND_MARKERS = (b"The specified key does not exist", b"NoSuchKey", b"[code: 10007]")
NOT_KEY_MARKERS = (b"[code: 10006]", b"NoSuchBucket", b"Authentication", b"Unauthorized", b"[code: 10000]")
WORK_MARKER = ".provenage-publish-workdir"
# The builder is offline and needs almost nothing from the environment. An allowlist keeps CLOUDFLARE_* and any other
# token-like variable out of it.
BUILDER_ENV_KEEP = ("PATH", "HOME", "TMPDIR", "TZ", "TZDIR", "LANG", "LANGUAGE", "SYSTEMROOT", "VIRTUAL_ENV")
INVALID = object()  # the previous index exists but cannot be compared

OK, E_BUILD, E_USAGE, E_LOCK, E_SHRINK, E_LEDGER, E_STORAGE, E_INTERNAL = range(8)


class NotFound(Exception):
    pass


class StorageError(Exception):
    pass


class WranglerStore:
    """R2 access through the wrangler CLI. Never uses `get --file`: a missing key leaves a 0-byte file behind and
    exits non-zero, which a careless reader would take for an empty object. `--pipe` returns bytes on stdout."""

    def __init__(self, bucket, target, persist_to=None, wrangler=WRANGLER):
        self.bucket, self.target, self.persist_to = bucket, target, persist_to
        self.wrangler = wrangler

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
        if is_not_found(p.stdout + p.stderr):
            raise NotFound(key)
        raise StorageError("wrangler get %s failed (exit %d): %s" % (key, p.returncode, self._tail(p)))

    def put(self, key, data, content_type):
        try:
            fd, path = tempfile.mkstemp(prefix="provenage-put-")  # private (0600) temporary file, removed below
        except OSError as e:
            raise StorageError("cannot create a temporary file for %s: %s" % (key, e)) from None
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            p = self._run(["r2", "object", "put", "%s/%s" % (self.bucket, key), "--file", path,
                           "--content-type", content_type] + self._flags())
        except OSError as e:
            raise StorageError("wrangler put %s failed: %s" % (key, e)) from None
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        if p.returncode != 0:
            raise StorageError("wrangler put %s failed (exit %d): %s" % (key, p.returncode, self._tail(p)))


def is_not_found(output):
    """True only when wrangler's output positively names a missing key. Unknown output is never "not found"."""
    text = ANSI_RE.sub(b"", output)
    if any(m in text for m in NOT_KEY_MARKERS) or b"bucket does not exist" in text.lower():
        return False
    return any(m in text for m in NOT_FOUND_MARKERS)


def builder_env():
    """The environment for the builder subprocess: an allowlist, so no CLOUDFLARE_* or other credential-like
    variable reaches it. PYTHON* variables are dropped too (the builder also runs with -E)."""
    keep = {k: v for k, v in os.environ.items() if k in BUILDER_ENV_KEEP or k.startswith("LC_")}
    return keep


def sha(data):
    return hashlib.sha256(data).hexdigest()


def run_builder(argv, env):
    """Run the builder subprocess with cwd set to the package directory, so `-m meeting_ics` resolves there and a
    stray meeting_ics/ in the operator's current directory cannot shadow it. A function so tests can wrap it."""
    return subprocess.run(argv, env=env, cwd=PKG, stdin=subprocess.DEVNULL, capture_output=True, check=False)


def _prev_ok(prev):
    if not isinstance(prev, dict) or prev.get("schema") != INDEX_SCHEMA:
        return False
    totals, feeds = prev.get("totals"), prev.get("feeds")
    if not isinstance(totals, dict) or isinstance(totals.get("events"), bool) \
            or not isinstance(totals.get("events"), int) or not isinstance(feeds, list):
        return False
    if not all(isinstance(f, dict) and isinstance(f.get("key"), str) and isinstance(f.get("sha256"), str)
               and (f.get("kind") != "combined" or (isinstance(f.get("jurisdiction"), str)
                                                    and isinstance(f.get("events"), int)))
               for f in feeds):
        return False
    # Optional additions (0.3.0 indexes). An index from before them has neither key and is still comparable.
    aliases, jurs = prev.get("aliases"), prev.get("jurisdictions")
    if aliases is not None and not (isinstance(aliases, dict) and all(
            isinstance(aliases.get(k, {}), dict) for k in ("bodies", "jurisdictions"))):
        return False
    return jurs is None or (isinstance(jurs, dict) and all(isinstance(v, dict) for v in jurs.values()))


def _aliases_of(index):
    a = index.get("aliases") or {}
    return {k: a.get(k) or {} for k in ("bodies", "jurisdictions")}


def _covered(j, b, n_al, new_keys):
    """Follow the Worker's resolution order (5.3): is a vanished feed j/b retired or moved to a feed still published?"""
    # 5.3 step 2: a body alias wins (null = retired; the builder guarantees a non-null target is a current feed)
    if b != "all" and "%s/%s" % (j, b) in n_al["bodies"]:
        return True
    # 5.3 steps 1-2: otherwise the jurisdiction alias applies; null = retired, else the moved key must exist
    if j in n_al["jurisdictions"]:
        t = n_al["jurisdictions"][j]
        return t is None or "calendar/%s/%s.ics" % (t, b) in new_keys
    return False


def alias_problems(prev, new) -> List[str]:
    """Permanence guards for a readable previous index: aliases are append-only, and no non-dev feed key may vanish
    unless an alias covers it. A feed in a jurisdiction the PREVIOUS index marks status "dev" is exempt (its slug
    carries no permanence promise); the new index cannot grant itself the exemption."""
    problems = []
    p_al, n_al = _aliases_of(prev), _aliases_of(new)
    for kind in ("bodies", "jurisdictions"):
        for src in sorted(p_al[kind]):
            if src not in n_al[kind]:
                problems.append("alias %s %s was removed (aliases are append-only)" % (
                    "body" if kind == "bodies" else "jurisdiction", src))
            elif n_al[kind][src] != p_al[kind][src]:
                problems.append("alias %s %s was changed from %s to %s (aliases are append-only)" % (
                    "body" if kind == "bodies" else "jurisdiction", src, json.dumps(p_al[kind][src]),
                    json.dumps(n_al[kind][src])))
    dev = {j for j, v in (prev.get("jurisdictions") or {}).items() if v.get("status") == "dev"}
    new_keys = {f["key"] for f in new["feeds"]}
    for f in sorted(prev["feeds"], key=lambda e: e["key"]):
        k = f["key"]
        if k in new_keys:
            continue
        m = FEED_KEY_RE.match(k)
        if not m:
            continue  # cannot be a key this publisher produced; nothing to protect
        j, b = k.split("/")[1], k.split("/")[2][:-4]
        if j in dev:
            continue
        if _covered(j, b, n_al, new_keys):
            continue
        problems.append("feed %s disappeared without an alias" % k)
    return problems


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
        if j not in new_comb and j in _aliases_of(new)["jurisdictions"]:
            continue  # renamed or retired through an alias; alias_problems checks every feed key is covered
        if j not in new_comb:
            problems.append("jurisdiction %s disappeared (%d events)" % (j, p))
        elif p > 0 and new_comb[j] * 2 < p:
            problems.append("jurisdiction %s events dropped from %d to %d (more than 50%%)" % (j, p, new_comb[j]))
    return problems + alias_problems(prev, new)


def _fail(code, msg):
    print(msg, file=sys.stderr)
    return code


def _open_nofollow(path, mode):
    """Create or truncate a regular file for writing without following a symlink at the final component."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("%s is not a regular file" % path)
    except BaseException:
        os.close(fd)
        raise
    return os.fdopen(fd, mode, encoding="utf-8") if "b" not in mode else os.fdopen(fd, mode)


def prepare_work_dir(path):
    """Return (work_dir, made). With no path, a fresh private temporary directory. With a path, it must not exist, or
    be empty, or carry the marker file this script wrote; otherwise nothing in it is touched."""
    if path is None:
        return tempfile.mkdtemp(prefix="provenage-publish-"), True
    path = os.path.abspath(path)
    marker = os.path.join(path, WORK_MARKER)
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        st = None
    if st is not None:
        if not stat.S_ISDIR(st.st_mode):  # lstat: a symlink is not a directory here
            raise ValueError("--work-dir %s exists and is not a plain directory" % path)
        if st.st_uid != os.getuid() or st.st_mode & 0o022:
            raise ValueError("--work-dir %s is not owned by you or is group/other-writable; refusing to use it" % path)
        try:
            mst = os.lstat(marker)
        except FileNotFoundError:
            mst = None
        if mst is not None and not stat.S_ISREG(mst.st_mode):
            raise ValueError("--work-dir marker %s is not a regular file; refusing to use it" % marker)
        if mst is None and os.listdir(path):
            raise ValueError("--work-dir %s is not empty and was not created by this script (no %s file); refusing "
                             "to use it. Pass a new or empty directory, or omit --work-dir" % (path, WORK_MARKER))
    else:
        os.makedirs(path, mode=0o700)
    with _open_nofollow(marker, "w") as f:
        f.write("created by build_and_publish.py; only <this directory>/out and ledger.json are rewritten\n")
    return path, False


def lock_path_for(args):
    """The lock file path. The default lives in a private per-user directory, not directly in the shared temp dir."""
    if args.lock_file:
        return os.path.abspath(args.lock_file)
    d = os.path.join(tempfile.gettempdir(), "provenage-publish-%d" % os.getuid())
    try:
        os.mkdir(d, 0o700)
    except FileExistsError:
        pass
    st = os.lstat(d)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise ValueError("lock directory %s is not a private directory owned by you; remove it or pass --lock-file"
                         % d)
    return os.path.join(d, "%s-%s.lock" % (args.bucket, args.target))


def open_lock(path):
    """Open the lock file without following a symlink (O_NOFOLLOW) and check it is a regular file we own."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
            raise ValueError("lock file %s is not a regular file owned by you" % path)
    except BaseException:
        os.close(fd)
        raise
    return os.fdopen(fd, "r+")


def run(args, store, out=None) -> int:
    """The whole publish. `store` has get(key) and put(key, data, content_type). Returns the exit code.
    Nothing escapes as a Python traceback with exit 1: setup problems are 2, storage and file-system problems 6,
    anything unexpected 7."""
    out = out or sys.stdout

    def say(s):
        print(s, file=out)

    work, made_work, lock = None, False, None
    try:
        try:
            work, made_work = prepare_work_dir(args.work_dir)
            lock_path = lock_path_for(args)
            lock = open_lock(lock_path)
        except (OSError, ValueError) as e:
            return _fail(E_USAGE, "error: cannot set up the work directory or lock file: %s" % e)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return _fail(E_LOCK, "another publish is running (lock %s)" % lock_path)
        return _locked(args, store, work, say)
    except (OSError, StorageError, NotFound) as e:
        return _fail(E_STORAGE, "storage or file-system error: %s" % e)
    except Exception as e:  # noqa: BLE001 - last resort: a documented code and a clear message, never exit 1
        return _fail(E_INTERNAL, "internal error (%s: %s). This is a bug in build_and_publish.py or unexpected "
                     "builder output, not a build failure. The bucket may be partly updated; rerun with --dry-run "
                     "to see what is left"
                     % (type(e).__name__, e))
    finally:
        if lock is not None:
            lock.close()
        if made_work and work:
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
        if args.target == "remote" and not args.init_ledger:
            return _fail(E_LEDGER, "state/ledger.json was not found in the remote bucket. The first remote publish "
                         "needs --init-ledger (it starts SEQUENCE at 0), and so does a bucket whose ledger went "
                         "missing; check the bucket name first")
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
    for planted in (out_dir, ledger_path):
        if os.path.islink(planted):
            return _fail(E_USAGE, "error: %s is a symlink in the work directory; refusing to write through it"
                         % planted)
    shutil.rmtree(out_dir, ignore_errors=True)
    try:
        ledger_f = _open_nofollow(ledger_path, "wb")
    except (OSError, ValueError) as e:
        return _fail(E_USAGE, "error: cannot write %s safely: %s" % (ledger_path, e))
    with ledger_f as f:
        f.write(old_raw if old_raw is not None else b"{}\n")
    # -E ignores PYTHON* variables; cwd is the package directory (see run_builder); every path is absolute.
    cmd = [sys.executable, "-E", "-m", "meeting_ics", "build-set", "--input", os.path.abspath(args.input),
           "--config", os.path.abspath(args.config), "--ledger", ledger_path, "--out", out_dir]
    if args.adapter:
        cmd += ["--adapter", args.adapter]
    if args.uid_namespace:
        cmd += ["--uid-namespace", args.uid_namespace]
    if args.retrieved_at:
        cmd += ["--retrieved-at", args.retrieved_at]
    p = run_builder(cmd, builder_env())
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
        ev = index["totals"]["events"]
        if isinstance(ev, bool) or not isinstance(ev, int):
            raise ValueError("built index totals.events is not an integer")
        for ent in index["feeds"]:
            if ent["kind"] == "combined" and not (isinstance(ent["jurisdiction"], str)
                                                  and isinstance(ent["events"], int)):
                raise ValueError("built index has a malformed combined feed entry")
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
    ap.add_argument("--force", action="store_true", help="publish despite a guard refusal; each override is printed")
    ap.add_argument("--init-ledger", action="store_true",
                    help="start an empty ledger although calendar/index.json exists (resets SEQUENCE)")
    ap.add_argument("--work-dir", help="scratch directory: must be new or empty, or carry the marker file this "
                    "script wrote (default: a fresh private temporary directory, removed afterwards)")
    ap.add_argument("--lock-file", help="lock file path (default: a private per-user directory under the temp "
                    "directory, per bucket and target)")
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
        err = check_public_base(a.public_base)
        if err:
            return err
    a.input, a.config = os.path.abspath(a.input), os.path.abspath(a.config)
    return None


def check_public_base(url) -> Optional[str]:
    msg = "--public-base must be an https URL with a host name, no user name or password, query or fragment"
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 or c == "\\" for c in url):
        return msg
    try:
        u = urllib.parse.urlsplit(url)
        host, _port = u.hostname, u.port
    except ValueError:
        return msg
    if u.scheme != "https" or not host or u.username is not None or u.password is not None or "@" in u.netloc \
            or u.query or u.fragment or "?" in url or "#" in url:
        return msg
    return None


def main(argv=None) -> int:
    a = parse_args(argv)
    err = check_args(a)
    if err:
        return _fail(E_USAGE, "error: " + err)
    if not os.path.isfile(WRANGLER):
        return _fail(E_USAGE, "error: wrangler not found at %s; run `npm ci` in workers/feeds" % WRANGLER)
    store = WranglerStore(a.bucket, a.target, a.persist_to)
    return run(a, store)


if __name__ == "__main__":
    sys.exit(main())
