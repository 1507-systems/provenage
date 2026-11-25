"""Regenerate the /mix/v1/ test fixtures with the real feed builder (synthetic, fictional data only).

Usage (from the repository root):  PYTHONPATH=packages/meeting-ics python3 workers/feeds/scripts/gen_mix_fixtures.py
Writes workers/feeds/test/fixtures/calendar/... and calendar/index.json. CI regenerates into a temporary
directory and fails if the committed fixtures differ, so they never drift from the builder.
"""
import json
import os
import sys

from meeting_ics import BuildOptions, build_feedset
from meeting_ics.feedset import write_feedset
from meeting_ics.ical import fold

OPTS = BuildOptions(uid_domain="example.org")
NAMES = {"alpha-town": "Alpha Town", "beta-county": "Beta County Government"}

# Hostile body name: CR, LF, comma, semicolon, backslash and markup. It must only ever appear escaped.
HOSTILE = "Parks, Rec; Board \\ <script>alert(1)</script>"


def rec(j, rid, body, slug, date, start, mtype, status="scheduled", **kw):
    d = {"uid_namespace": j, "record_id": rid, "body_name": body, "meeting_type": mtype, "date": date,
         "start_time": start, "timezone": "America/Denver", "status": status,
         "retrieved_at": "2026-03-01T06:00:00Z", "jurisdiction_slug": j, "body_slug": slug}
    d.update(kw)
    return d


def records():
    long_type = "Special Called Joint Work Session With Neighbouring Districts On Zoning Overlay"
    return [
        rec("alpha-town", "A1", "City Council", "city-council", "2026-03-02", "18:00", "City Council Regular Meeting"),
        rec("alpha-town", "A2", "City Council", "city-council", "2026-03-16", "18:00", "Study Session",
            location="Council Chambers, 1 Main St"),
        rec("alpha-town", "A3", "Planning Commission", "planning-commission", "2026-03-09", "19:00",
            "Regular Meeting", status="cancelled"),
        rec("alpha-town", "A4", "Planning Commission", "planning-commission", "2026-03-23", "19:00", long_type),
        rec("beta-county", "B1", "Board of Commissioners", "board-of-commissioners", "2026-03-03", "09:00",
            "City Council Regular Meeting"),
        rec("beta-county", "B2", "Board of Commissioners", "board-of-commissioners", "2026-03-17", "09:00",
            "Café Licensing Hearing"),
        rec("beta-county", "B3", HOSTILE, "parks-board", "2026-03-05", "17:30", "Regular Meeting"),
    ]


def build():
    from meeting_ics.feedset import SetMeta
    meta = SetMeta({"alpha-town": "Alpha", "beta-county": "Beta Co"}, {"beta-county": "dev"},
                   {"bodies": {"alpha-town/old-commission": "alpha-town/planning-commission",
                               "alpha-town/retired-board": None},
                    "jurisdictions": {"gamma-town": "alpha-town", "lost-town": None}})
    return build_feedset(records(), OPTS, None, NAMES, meta)


def fold_vectors():
    inputs = ["", "short", "x" * 75, "x" * 76, "x" * 150, "x" * 151, "SUMMARY:" + "\u00e9" * 40,
              "\u00e9" * 37 + "ab", "\u00e9" * 38, "a" + "\u20ac" * 30, "\U0001f600" * 20, "a" * 74 + "\U0001f600",
              "X-WR-CALNAME:Alpha Town\\, Planning \u2014 Special Called Joint Work Session With Neighbours"]
    return [{"input": i, "folded": fold(i)} for i in inputs]


def main(out):
    write_feedset(build(), out, overwrite=True)
    with open(os.path.join(out, "fold-vectors.json"), "w", encoding="utf-8") as fh:
        json.dump(fold_vectors(), fh, indent=1, ensure_ascii=True)
        fh.write("\n")


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(here, "..", "test", "fixtures"))
