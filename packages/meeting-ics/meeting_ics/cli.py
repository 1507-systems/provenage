"""CLI: python3 -m meeting_ics build --input IN --output OUT --ledger LEDGER [--config JSON] [--adapter NAME]

--uid-namespace is required with --adapter.
--input is a records JSON (list or {"records": [...]}) unless --adapter names a module in meeting_ics.adapters
(e.g. a module you wrote), in which case it is that source's file.
--config is a JSON object of BuildOptions fields (prodid, uid_domain, default_duration_hours|_minutes, end_rules,
x_prefix, detail, calendar_name). end_rules entries: [substring, "HH:MM"] or {"match":..,"end_local":..,"basis":..}.
"""
import argparse
import importlib
import json
import os
import sys

from .build import BuildOptions, build_with_report
from .ledger import Ledger
from .model import records_from_json


def run(input_path, output_path, ledger_path, options, adapter=None, uid_namespace=None):
    if adapter:
        if not uid_namespace:
            raise SystemExit("--uid-namespace is required with --adapter")
        mod = importlib.import_module("meeting_ics.adapters." + adapter)
        records = mod.load(input_path, uid_namespace=uid_namespace)
    else:
        with open(input_path, encoding="utf-8") as f:
            records = records_from_json(json.load(f))
    ledger = Ledger.load(ledger_path)
    data, rep = build_with_report(records, options, ledger)
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "wb") as f:
        f.write(data)
    ledger.save(ledger_path)
    return rep


def main(argv=None):
    ap = argparse.ArgumentParser(prog="meeting_ics")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--input", required=True)
    b.add_argument("--output", required=True)
    b.add_argument("--ledger", required=True)
    b.add_argument("--config")
    b.add_argument("--adapter")
    b.add_argument("--uid-namespace", help="required with --adapter; prefix of every UID")
    a = ap.parse_args(argv)
    cfg = {}
    if a.config:
        with open(a.config, encoding="utf-8") as f:
            cfg = json.load(f)
    rep = run(a.input, a.output, a.ledger, BuildOptions.from_dict(cfg), a.adapter, a.uid_namespace)
    bumped = ",".join(rep["bumped"]) or "none"
    print("events=%d cancelled=%d sequence_bumped=%s" % (rep["events"], rep["cancelled"], bumped))
    return 0


if __name__ == "__main__":
    sys.exit(main())
