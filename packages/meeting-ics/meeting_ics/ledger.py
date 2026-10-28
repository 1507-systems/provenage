"""SEQUENCE ledger (rules R10/R11).

Per UID: critical_hash, docs_hash, sequence, last_modified.
  first sight: sequence 0, last_modified = DTSTAMP.
  if either hash differs: sequence += 1, last_modified = this run's DTSTAMP. Otherwise unchanged.
  UIDs absent from the input stay in the ledger (never pruned).
"""
import hashlib
import json
import os


def sha(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
                          .encode("utf-8")).hexdigest()


class Ledger:
    def __init__(self, entries=None):
        self.entries = entries if entries is not None else {}

    @classmethod
    def load(cls, path):
        try:
            with open(path, encoding="utf-8") as f:
                return cls(json.load(f))
        except FileNotFoundError:
            return cls()

    def save(self, path):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(self.entries, f, indent=1, sort_keys=True)
            f.write("\n")

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
