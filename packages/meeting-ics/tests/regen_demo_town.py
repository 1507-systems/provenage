"""Regenerate (or check) the golden files under tests/fixtures/demo-town/expected/.

    python3 tests/regen_demo_town.py            rewrite expected/ from the CSVs and options.json
    python3 tests/regen_demo_town.py --check    exit 1 if expected/ differs from a fresh build; writes nothing

Run it from packages/meeting-ics. After a regeneration, review `git diff`: a changed golden file is a changed feed.
"""
import os
import shutil
import sys

PKG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PKG not in sys.path:
    sys.path.insert(0, PKG)

from tests import demo_town  # noqa: E402  (needs PKG on sys.path first)


def stored():
    found = {}
    for version, _csv, _stamp in demo_town.VERSIONS:
        demo_town.read_tree(os.path.join(demo_town.EXPECTED, version), version, found)
    return found


def main(argv):
    if argv not in ([], ["--check"]):
        print("usage: regen_demo_town.py [--check]", file=sys.stderr)
        return 2
    built = demo_town.build_all()
    if argv:
        have = stored()
        bad = sorted(k for k in set(built) | set(have) if built.get(k) != have.get(k))
        for k in bad:
            print("differs: " + k)
        print("demo-town golden files: %s" % ("%d differ" % len(bad) if bad else "up to date"))
        return 1 if bad else 0
    shutil.rmtree(demo_town.EXPECTED, ignore_errors=True)
    for rel, data in sorted(built.items()):
        path = os.path.join(demo_town.EXPECTED, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
    print("wrote %d files under tests/fixtures/demo-town/expected/" % len(built))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
