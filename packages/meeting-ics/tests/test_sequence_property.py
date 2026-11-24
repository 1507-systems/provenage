"""SEQUENCE is monotonic and bumps exactly when a hashed field changes (seeded random walk)."""
import random
import unittest

from meeting_ics import BuildOptions, Ledger, build_with_report
from meeting_ics.check import parse_ics
from meeting_ics.ledger import monotonic_problems

from tests.helpers import rec

OPTS = BuildOptions(uid_domain="example.org")
STATUSES = ["scheduled", "rescheduled", "postponed", "completed", "cancelled"]
TIMES = ["18:00", "18:30", "19:00", "17:00"]


class Monotonic(unittest.TestCase):
    def test_random_walk(self):
        rng = random.Random(5545)
        uids = ["t-%d" % i for i in range(1, 6)]
        state = {u: {"start_time": "18:00", "status": "scheduled", "location": None, "video": None} for u in uids}
        present = set(uids)
        last = {}
        ledger = Ledger()

        def tup(s):
            return (s["start_time"], s["status"], s["location"], s["video"])

        for step in range(200):
            u = rng.choice(uids)
            action = rng.choice(["time", "status", "location", "video", "none", "drop"])
            s = state[u]
            if action == "time":
                s["start_time"] = rng.choice(TIMES)
            elif action == "status":
                s["status"] = rng.choice(STATUSES)
            elif action == "location":
                s["location"] = rng.choice([None, "Room A", "Room B"])
            elif action == "video":
                s["video"] = rng.choice([None, "https://v/1", "https://v/2"])
            elif action == "drop":
                present ^= {u}
            if not present:
                present.add(u)
            records = []
            for x in sorted(present):
                st = state[x]
                links = {"portal": "https://x/" + x}
                if st["video"]:
                    links["video"] = st["video"]
                records.append(rec(record_id=x.split("-")[1], start_time=st["start_time"], status=st["status"],
                                   location=st["location"], links=links))
            before = {k: dict(v) for k, v in ledger.entries.items()}
            data, rep = build_with_report(records, OPTS, ledger)
            self.assertEqual(monotonic_problems(before, ledger), [], step)
            cal = parse_ics(data)
            for x in present:
                uid = x + "@example.org"
                seq = ledger.entries[uid]["sequence"]
                self.assertEqual(int(cal.event(uid).get("SEQUENCE")), seq)
                if uid in last:
                    changed = last[uid][0] != tup(state[x])
                    self.assertEqual(seq, last[uid][1] + (1 if changed else 0), (step, uid))
                last[uid] = (tup(state[x]), seq)


if __name__ == "__main__":
    unittest.main()
