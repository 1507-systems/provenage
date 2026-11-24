"""CLI: python3 -m meeting_ics <command>

  build     --input IN --output OUT --ledger LEDGER [--config JSON] [--adapter NAME --uid-namespace NS]
  validate  --input IN [--adapter NAME --uid-namespace NS]

--uid-namespace is required with --adapter.
--input is a records JSON (list or {"records": [...], "schema_version": 1}) unless --adapter names a module in
meeting_ics.adapters (e.g. a module you wrote), in which case it is that source's file.
--config is a JSON object of BuildOptions fields (prodid, uid_domain, default_duration_hours|_minutes, end_rules,
x_prefix, detail, calendar_name). end_rules entries: [substring, "HH:MM"] or {"match":..,"end_local":..,"basis":..}.

Exit codes: 0 ok, 2 any error (invalid input, unreadable file, bad usage). Nothing is written on an error.
Input problems print one `<path>: <message>` line each to stderr, then a summary line.
"""
import argparse
import importlib
import json
import os
import re
import sys

from .build import BuildOptions, build_with_report
from .ledger import Ledger
from .validate import RecordError, load_input, validate_records


def load_records(input_path, adapter=None, uid_namespace=None):
    """Records from a generic JSON input, or from an adapter module. Raises RecordError on bad generic input."""
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
    return mod.load(input_path, uid_namespace=uid_namespace)


def run(input_path, output_path, ledger_path, options, adapter=None, uid_namespace=None):
    records = load_records(input_path, adapter, uid_namespace)
    ledger = Ledger.load(ledger_path)
    data, rep = build_with_report(records, options, ledger)  # validates; nothing is written before this succeeds
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "wb") as f:
        f.write(data)
    ledger.save(ledger_path)
    return rep


def _build(a):
    cfg = {}
    if a.config:
        with open(a.config, encoding="utf-8") as f:
            cfg = json.load(f)
    rep = run(a.input, a.output, a.ledger, BuildOptions.from_dict(cfg), a.adapter, a.uid_namespace)
    bumped = ",".join(rep["bumped"]) or "none"
    print("events=%d cancelled=%d sequence_bumped=%s" % (rep["events"], rep["cancelled"], bumped))


def _validate(a):
    records = validate_records(load_records(a.input, a.adapter, a.uid_namespace))
    print("ok records=%d" % len(records))


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
    b.set_defaults(func=_build, tail="nothing written")
    v = sub.add_parser("validate", help="check an input file; prints 'ok records=N' or every problem")
    v.add_argument("--input", required=True)
    v.add_argument("--adapter")
    v.add_argument("--uid-namespace", help="required with --adapter; prefix of every UID")
    v.set_defaults(func=_validate, tail="input is invalid")
    a = ap.parse_args(argv)
    try:
        a.func(a)
    except RecordError as e:
        for err in e.errors:
            print(err.line(), file=sys.stderr)
        print("meeting_ics: %d error(s); %s" % (len(e.errors), a.tail), file=sys.stderr)
        return 2
    except (ValueError, RuntimeError, OSError, NotImplementedError) as e:
        print("meeting_ics: error: %s" % e, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
