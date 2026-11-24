"""SEQUENCE ledger (rules R10/R11).

Per UID: critical_hash, docs_hash, sequence, last_modified.
  first sight: sequence 0, last_modified = DTSTAMP.
  if either hash differs: sequence += 1, last_modified = this run's DTSTAMP. Otherwise unchanged.
  UIDs absent from the input stay in the ledger (never pruned).
"""
import datetime
import hashlib
import json
import os
import tempfile
from typing import Dict, List

_KEYS = ("critical_hash", "docs_hash", "sequence", "last_modified")


def sha(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
                          .encode("utf-8")).hexdigest()


def _current_umask():
    mask = os.umask(0)
    os.umask(mask)
    return mask


def atomic_write(path, data):
    """Write bytes to a new, uniquely named temporary file in the same directory (created exclusively, so a
    pre-existing file or symlink is never followed), fsync, then os.replace onto path, so a reader never sees a
    half-written file. The temporary file is removed if anything fails."""
    path = os.fspath(path)
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=os.path.basename(path) + ".", suffix=".tmp")  # O_EXCL, 0600
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o666 & ~_current_umask())  # honour the caller's umask (mkstemp creates 0600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _bad(where, reason):
    return ValueError("malformed ledger: %s: %s" % (where, reason))


def _check_entry(uid, entry):
    if not isinstance(entry, dict):
        raise _bad(uid, "entry must be a JSON object")
    for key in _KEYS:
        if key not in entry:
            raise _bad(uid, "missing key %s" % key)
    for key in ("critical_hash", "docs_hash"):
        if not isinstance(entry[key], str):
            raise _bad(uid, "%s must be a string" % key)
    seq = entry["sequence"]
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
        raise _bad(uid, "sequence must be an integer >= 0, got %r" % (seq,))
    lm = entry["last_modified"]
    try:
        if not isinstance(lm, str):
            raise ValueError
        datetime.datetime.strptime(lm, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise _bad(uid, "last_modified must be a UTC timestamp YYYY-MM-DDTHH:MM:SSZ, got %r" % (lm,)) from None


def monotonic_problems(old, new) -> List[str]:
    """Problems if `new` is not a safe successor of `old` (a Ledger or an entries dict): a UID missing from the
    new ledger, or a SEQUENCE that went down. Either means a lost or reset ledger and must stop a publish."""
    o = getattr(old, "entries", old)
    n = getattr(new, "entries", new)
    problems = []
    for uid in sorted(o):
        if uid not in n:
            problems.append("uid %s missing from new ledger" % uid)
        elif n[uid]["sequence"] < o[uid]["sequence"]:
            problems.append("uid %s sequence decreased %d -> %d" % (uid, o[uid]["sequence"], n[uid]["sequence"]))
    return problems


class Ledger:
    def __init__(self, entries=None):
        self.entries: Dict[str, dict] = entries if entries is not None else {}

    @classmethod
    def loads(cls, data: bytes):
        """Parse ledger bytes. Anything that is not a well-formed ledger raises ValueError ('malformed ledger:
        ...'): a bad ledger must never be read as an empty one, because that would reset every SEQUENCE."""
        refuse = "; refusing to start a fresh ledger because that would reset SEQUENCE"
        try:
            obj = json.loads(data.decode("utf-8"))
        except UnicodeDecodeError as e:
            raise _bad("<root>", "not valid UTF-8 (%s)%s" % (e, refuse)) from None
        except ValueError as e:
            raise _bad("<root>", "not valid JSON (%s)%s" % (e, refuse)) from None
        if not isinstance(obj, dict):
            raise _bad("<root>", "top level must be a JSON object")
        for uid, entry in obj.items():
            _check_entry(uid, entry)
        return cls(obj)

    @classmethod
    def load(cls, path):
        """A missing file is a fresh ledger; any other problem (including an empty file) raises."""
        try:
            with open(path, "rb") as f:
                data = f.read()
        except FileNotFoundError:
            return cls()
        return cls.loads(data)

    def dumps(self) -> bytes:
        return (json.dumps(self.entries, indent=1, sort_keys=True) + "\n").encode("utf-8")

    def save(self, path):
        atomic_write(path, self.dumps())

    def update(self, uid, critical_hash, docs_hash, stamp):
        """Record this run's hashes for uid. Returns (entry, bumped)."""
        led = self.entries.get(uid)
        bumped = False
        if led is None:
            led = {"critical_hash": critical_hash, "docs_hash": docs_hash, "sequence": 0, "last_modified": stamp}
        elif led["critical_hash"] != critical_hash or led["docs_hash"] != docs_hash:
            led = {"critical_hash": critical_hash, "docs_hash": docs_hash,
                   "sequence": led["sequence"] + 1, "last_modified": stamp}
            bumped = True
        self.entries[uid] = led
        return led, bumped
