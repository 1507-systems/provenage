"""CLI: python3 -m meeting_ics <command>

  build     --input IN --output OUT --ledger LEDGER [--config JSON] [--adapter NAME --uid-namespace NS]
            [--retrieved-at TS|now]
  build-set --input IN --config SET.json --ledger LEDGER --out DIR [--overwrite] [--adapter NAME --uid-namespace NS]
            [--retrieved-at TS|now]
  validate  --input IN [--adapter NAME --uid-namespace NS] [--retrieved-at TS|now]
  check-feed FILE [--expect-events N]
  template  [--output PATH]

--uid-namespace is required with --adapter. --retrieved-at (UTC YYYY-MM-DDTHH:MM:SSZ, or now) fills blank
retrieved_at cells and is valid only with --adapter csv_template.
--input is a records JSON (list or {"records": [...], "schema_version": 1}) unless --adapter names a module in
meeting_ics.adapters (e.g. a module you wrote), in which case it is that source's file.
--config is a JSON object of BuildOptions fields (prodid, uid_domain, default_duration (ISO 8601, e.g. PT90M) or
default_duration_hours|_minutes, end_rules,
x_prefix, detail, calendar_name, calendar_description, method, refresh_interval). Unknown keys are an error.
end_rules entries: [substring, "HH:MM"] or {"match":..,"end_local":..,"basis":..}.

build-set groups records by jurisdiction_slug and body_slug (both required) and writes DIR/calendar/<j>/<b>.ics,
DIR/calendar/<j>/all.ics and DIR/calendar/index.json with one shared ledger. --config must set uid_domain and may
add jurisdictions: {slug: {"name": ...}} for feed titles; calendar_name is not allowed. A jurisdiction entry may also
carry uid_namespace, prodid, calendar_description, default_duration* and end_rules, which override the set-wide
values for that jurisdiction only. --input may be repeated (build-set and validate) to read several files.

check-feed checks a built .ics (strict parse, structure, escapes, no ATTENDEE) and exits 0 (ok), 1 (problems found)
or 2 (unreadable file). template writes the CSV header row for the csv_template adapter to stdout or --output.

Exit codes: 0 ok, 2 any error (invalid input, unreadable file, bad usage). Nothing is written on an error.
Input problems print one `<path>: <message>` line each to stderr, then a summary line.
"""
import argparse
import datetime
import importlib
import importlib.resources as resources
import json
import re
import sys

from . import check, feedset
from .build import BuildOptions, build_with_report
from .ledger import Ledger, atomic_write
from .validate import RecordError, load_input, validate_records


def load_records(input_path, adapter=None, uid_namespace=None, retrieved_at=None):
    """Records from a generic JSON input, or from an adapter module. Raises RecordError on bad generic input.
    retrieved_at ("now" or a UTC stamp) is only for the csv_template adapter."""
    if retrieved_at is not None and adapter != "csv_template":
        raise ValueError("--retrieved-at applies only to --adapter csv_template")
    if not adapter:
        return load_input(input_path)
    if not uid_namespace:
        raise ValueError("--uid-namespace is required with --adapter")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", adapter):
        raise ValueError("unknown adapter %r" % adapter)
    name = "meeting_ics.adapters." + adapter
    try:
        mod = importlib.import_module(name)
    except ModuleNotFoundError as e:
        if e.name == name:
            raise ValueError("unknown adapter %r" % adapter) from None
        raise
    if retrieved_at is None:
        return mod.load(input_path, uid_namespace=uid_namespace)
    if retrieved_at == "now":
        retrieved_at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return mod.load(input_path, uid_namespace=uid_namespace, retrieved_at=retrieved_at)


def load_many(inputs, adapter=None, uid_namespace=None, retrieved_at=None):
    """Records from one input path, or from several (a repeated --input), concatenated in the order given.
    With several files, every problem is reported with its file name in front; duplicate UIDs across files are
    refused later, when the records are validated together."""
    if isinstance(inputs, str) or len(inputs) == 1:
        return load_records(inputs if isinstance(inputs, str) else inputs[0], adapter, uid_namespace, retrieved_at)
    records, errors = [], []
    for path in inputs:
        try:
            records += load_records(path, adapter, uid_namespace, retrieved_at)
        except RecordError as e:
            for err in e.errors:
                err.path = "%s: %s" % (path, err.path)
            errors += e.errors
    if errors:
        errors[0].errors = errors
        raise errors[0]
    return records


def run(input_path, output_path, ledger_path, options, adapter=None, uid_namespace=None, retrieved_at=None):
    records = load_records(input_path, adapter, uid_namespace, retrieved_at)
    ledger = Ledger.load(ledger_path)
    data, rep = build_with_report(records, options, ledger)  # validates; nothing is written before this succeeds
    atomic_write(output_path, data)  # the .ics first, then the ledger: a failed ledger save rebuilds identically
    ledger.save(ledger_path)
    return rep


def _build(a):
    cfg = {}
    if a.config:
        with open(a.config, encoding="utf-8") as f:
            cfg = json.load(f)
    options = BuildOptions.from_dict(cfg)
    rep = run(a.input, a.output, a.ledger, options, a.adapter, a.uid_namespace, a.retrieved_at)
    if options.uid_domain == "example.invalid":
        print("meeting_ics: warning: uid_domain is example.invalid; set uid_domain in --config to a domain you "
              "control before publishing", file=sys.stderr)
    bumped = ",".join(rep["bumped"]) or "none"
    print("events=%d cancelled=%d sequence_bumped=%s" % (rep["events"], rep["cancelled"], bumped))


def _build_set(a):
    with open(a.config, encoding="utf-8") as f:
        options, names, meta = feedset.load_set_meta(json.load(f))
    records = load_many(a.input, a.adapter, a.uid_namespace, a.retrieved_at)
    ledger = Ledger.load(a.ledger)
    fs = feedset.build_feedset(records, options, ledger, names, meta)  # nothing is written before this succeeds
    feedset.write_feedset(fs, a.out, a.overwrite)
    fs.ledger.save(a.ledger)  # last: a failed ledger save leaves feeds that rebuild identically
    t = fs.index["totals"]
    print("feeds=%d events=%d cancelled=%d sequence_bumped=%s" % (t["feeds"], t["events"], t["cancelled"],
                                                                  ",".join(fs.bumped) or "none"))


def _validate(a):
    records = validate_records(load_many(a.input, a.adapter, a.uid_namespace, a.retrieved_at))
    print("ok records=%d" % len(records))


def _check_feed(a):
    with open(a.file, "rb") as f:
        data = f.read()
    errors, notes = check.validate(data, a.expect_events)
    try:
        events = len(check.parse_ics(data).events)
    except check.IcsFormatError:
        events = 0
    for e in errors:
        print("FAIL: " + e)
    for n in notes:
        print("NOTE: " + n)
    print("%s: events=%d failures=%d" % (a.file, events, len(errors)))
    return 1 if errors else 0


def _template(a):
    text = resources.files("meeting_ics.adapters").joinpath("csv_template.csv").read_text(encoding="utf-8")
    if not a.output:
        sys.stdout.write(text)
        return 0
    try:
        with open(a.output, "x", encoding="utf-8", newline="\n") as f:
            f.write(text)
    except FileExistsError:
        raise ValueError("refusing to overwrite %s" % a.output) from None
    return 0


RETRIEVED_HELP = ("csv_template only: UTC fetch time YYYY-MM-DDTHH:MM:SSZ (or 'now') for rows whose retrieved_at "
                  "is blank")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="meeting_ics")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="build a feed (validates first; writes nothing on error)")
    b.add_argument("--input", required=True)
    b.add_argument("--output", required=True)
    b.add_argument("--ledger", required=True)
    b.add_argument("--config")
    b.add_argument("--adapter")
    b.add_argument("--uid-namespace", help="required with --adapter; prefix of every UID")
    b.add_argument("--retrieved-at", help=RETRIEVED_HELP)
    b.set_defaults(func=_build, tail="nothing written")
    s = sub.add_parser("build-set", help="build per-body feeds, a combined feed per jurisdiction and an index")
    s.add_argument("--input", required=True, action="append",
                   help="records file; repeat it to build one set from several files (e.g. one CSV per jurisdiction)")
    s.add_argument("--config", required=True, help="BuildOptions keys (uid_domain required) plus jurisdictions")
    s.add_argument("--ledger", required=True)
    s.add_argument("--out", required=True, help="output directory; feeds go under OUT/calendar/")
    s.add_argument("--overwrite", action="store_true", help="replace a non-empty OUT/calendar")
    s.add_argument("--adapter")
    s.add_argument("--uid-namespace", help="required with --adapter; prefix of every UID")
    s.add_argument("--retrieved-at", help=RETRIEVED_HELP)
    s.set_defaults(func=_build_set, tail="nothing written")
    v = sub.add_parser("validate", help="check an input file; prints 'ok records=N' or every problem")
    v.add_argument("--input", required=True, action="append", help="records file; may be repeated")
    v.add_argument("--adapter")
    v.add_argument("--uid-namespace", help="required with --adapter; prefix of every UID")
    v.add_argument("--retrieved-at", help=RETRIEVED_HELP)
    v.set_defaults(func=_validate, tail="input is invalid")
    c = sub.add_parser("check-feed", help="check a built .ics file; exit 1 if it has problems")
    c.add_argument("file")
    c.add_argument("--expect-events", type=int, metavar="N")
    c.set_defaults(func=_check_feed, tail="")
    t = sub.add_parser("template", help="write the CSV template header row (stdout, or --output PATH)")
    t.add_argument("--output")
    t.set_defaults(func=_template, tail="")
    a = ap.parse_args(argv)
    try:
        return a.func(a) or 0
    except RecordError as e:
        for err in e.errors:
            print(err.line(), file=sys.stderr)
        print("meeting_ics: %d error(s); %s" % (len(e.errors), a.tail), file=sys.stderr)
        return 2
    except (ValueError, TypeError, RuntimeError, OSError, NotImplementedError) as e:
        print("meeting_ics: error: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
