"""Feed set: one jurisdiction's records grouped into per-body feeds plus a combined feed, and a feed index.

Layout (keys under the output directory, and later in the bucket):

  calendar/<jurisdiction>/<body>.ics   one feed per body
  calendar/<jurisdiction>/all.ics      every meeting of the jurisdiction
  calendar/index.json                  schema provenage-calendar-index/1

One ledger covers the whole set. It is keyed by UID, and a meeting has the same UID in its body feed and in
all.ics, so both always carry the same SEQUENCE. Standard library only.
"""
import copy
import dataclasses
import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass, field, replace
from typing import Dict, List, Optional, Tuple

from . import check
from .build import BuildOptions, build_with_report
from .ledger import Ledger, atomic_write
from .validate import SLUG_RE, UID_NAMESPACE_RE, validate_records  # noqa: F401  (SLUG_RE re-exported for callers)

COMBINED_SLUG = "all"
INDEX_KEY = "calendar/index.json"
INDEX_SCHEMA = "provenage-calendar-index/1"
MAX_NAME = 200
MAX_SHORT_NAME = 40
STATUSES = ("live", "dev")
# Build options a jurisdiction may override (the rest stay set-wide). Only keys actually given are overridden.
OVERRIDE_KEYS = ("prodid", "calendar_description", "default_duration", "default_duration_hours",
                 "default_duration_minutes", "end_rules")
ENTRY_KEYS = {"name", "short_name", "status", "uid_namespace"} | set(OVERRIDE_KEYS)
# Published in the index so consumers (the builder page, the /mix/ route) read limits from data, not code.
MIX = {"issue_max_bytes": 2000000, "max_refs": 32, "max_selection_bytes": 1536, "path": "mix/v1/", "version": 1}


@dataclass(frozen=True)
class FeedFile:
    key: str                  # "calendar/<j>/<b>.ics"
    data: bytes
    kind: str                 # "body" | "combined"
    jurisdiction: str
    body: str                 # body slug; "all" when kind == "combined"
    body_name: Optional[str]  # None for the combined feed
    title: str                # the X-WR-CALNAME value
    events: int
    cancelled: int


@dataclass
class FeedSet:
    feeds: List[FeedFile]     # sorted by key
    index: dict
    index_bytes: bytes
    ledger: Ledger            # a new ledger; the one passed in is never mutated
    bumped: List[str]         # sorted unique UIDs whose SEQUENCE went up


@dataclass(frozen=True)
class SetMeta:
    """Per-jurisdiction settings and aliases from a set config. Defaults reproduce the 0.2.0 index."""
    short_names: Dict[str, str]
    statuses: Dict[str, str]
    aliases: dict            # {"bodies": {"j/b": "j/b" | None}, "jurisdictions": {"j": "j" | None}}
    # Optional per-jurisdiction overrides; a jurisdiction absent here uses the set-wide values.
    uid_namespaces: Dict[str, str] = field(default_factory=dict)   # replaces the records' uid_namespace
    overrides: Dict[str, dict] = field(default_factory=dict)       # BuildOptions field -> value (normalised)


def _empty_aliases() -> dict:
    return {"bodies": {}, "jurisdictions": {}}


def _single_line(v, what, limit):
    if not isinstance(v, str) or not v.strip() or len(v) > limit or re.search(r"[\x00-\x1f\x7f]", v):
        raise ValueError("%s must be non-empty single-line text (max %d)" % (what, limit))


def _is_slug(v) -> bool:
    return isinstance(v, str) and len(v) <= 64 and re.fullmatch(SLUG_RE, v) is not None


def _check_name(slug, name):
    if (not isinstance(name, str) or not name.strip() or len(name) > MAX_NAME
            or re.search(r"[\x00-\x1f\x7f]", name)):
        raise ValueError("jurisdictions.%s.name must be non-empty single-line text (max %d)" % (slug, MAX_NAME))


def _check_namespace(slug, ns):
    if not (isinstance(ns, str) and re.fullmatch(UID_NAMESPACE_RE, ns) and len(ns) <= 128):
        raise ValueError("jurisdictions.%s.uid_namespace must be letters, digits and . _ - (start with a letter "
                         "or digit, max 128), got %r" % (slug, ns))


def _parse_overrides(slug, entry) -> dict:
    """The BuildOptions fields a jurisdiction entry overrides, normalised the way the set-wide config is."""
    sub = {k: entry[k] for k in OVERRIDE_KEYS if k in entry}
    if not sub:
        return {}
    try:
        parsed = BuildOptions.from_dict(sub)
    except ValueError as e:
        raise ValueError("jurisdictions.%s: %s" % (slug, e)) from None
    out = {}
    for k in sub:
        name = "default_duration" if k.startswith("default_duration") else k
        out[name] = getattr(parsed, name)
    return out


def _parse_aliases(raw) -> dict:
    out = _empty_aliases()
    if raw is None:
        return out
    if not isinstance(raw, dict) or not set(raw) <= {"bodies", "jurisdictions"}:
        raise ValueError('aliases must be an object with optional keys "bodies" and "jurisdictions"')
    for kind in ("bodies", "jurisdictions"):
        m = raw.get(kind, {})
        if not isinstance(m, dict):
            raise ValueError("aliases.%s must be an object" % kind)
        for src, dst in m.items():
            for label, v in (("source", src), ("target", dst)):
                if v is None and label == "target":
                    continue  # null target = retired
                parts = v.split("/") if isinstance(v, str) else None
                want = 2 if kind == "bodies" else 1
                if not (parts and len(parts) == want and all(_is_slug(x) for x in parts)):
                    raise ValueError("aliases.%s: %s %r must be %s" % (
                        kind, label, v, "<jurisdiction>/<body> slugs" if want == 2 else "a slug"))
                if kind == "bodies" and parts[1] == COMBINED_SLUG:
                    raise ValueError("aliases.bodies: %r names the reserved body 'all'" % (v,))
            out[kind][src] = dst
    return out


def load_set_meta(d) -> Tuple[BuildOptions, Dict[str, str], SetMeta]:
    """Split a build-set config into (BuildOptions, {jurisdiction slug: display name}, SetMeta)."""
    if not isinstance(d, dict):
        raise ValueError("--config must be a JSON object")
    d = dict(d)
    raw = d.pop("jurisdictions", None)
    aliases = _parse_aliases(d.pop("aliases", None))
    names: Dict[str, str] = {}
    short_names: Dict[str, str] = {}
    statuses: Dict[str, str] = {}
    uid_namespaces: Dict[str, str] = {}
    overrides: Dict[str, dict] = {}
    if raw is not None:
        if not isinstance(raw, dict):
            raise ValueError('jurisdictions must be an object like {"demo-town": {"name": "Demo Town"}}')
        for slug, entry in raw.items():
            if not (isinstance(slug, str) and len(slug) <= 64 and re.fullmatch(SLUG_RE, slug)):
                raise ValueError("jurisdictions: key %r must be a slug (lowercase letters, digits, single hyphens)"
                                 % (slug,))
            if not isinstance(entry, dict) or "name" not in entry or not set(entry) <= ENTRY_KEYS:
                raise ValueError('jurisdictions.%s must be an object with "name" and optionally "short_name", '
                                 '"status", "uid_namespace" and the overrides: %s' % (slug, ", ".join(OVERRIDE_KEYS)))
            name = entry["name"]
            _check_name(slug, name)
            names[slug] = name
            if "uid_namespace" in entry:
                _check_namespace(slug, entry["uid_namespace"])
                uid_namespaces[slug] = entry["uid_namespace"]
            ov = _parse_overrides(slug, entry)
            if ov:
                overrides[slug] = ov
            if "short_name" in entry:
                _single_line(entry["short_name"], "jurisdictions.%s.short_name" % slug, MAX_SHORT_NAME)
                short_names[slug] = entry["short_name"]
            if "status" in entry:
                if entry["status"] not in STATUSES:
                    raise ValueError("jurisdictions.%s.status must be one of: %s" % (slug, ", ".join(STATUSES)))
                statuses[slug] = entry["status"]
    if "calendar_name" in d:
        raise ValueError("calendar_name is set per feed by build-set; remove it from --config")
    if not d.get("uid_domain"):
        raise ValueError("build-set requires uid_domain in --config (UIDs must be globally unique and stable)")
    opts = BuildOptions.from_dict(d)
    for slug, ov in overrides.items():  # fail at load time, naming the jurisdiction, not mid-build
        _validate_override(slug, replace(opts, **ov))
    return opts, names, SetMeta(short_names, statuses, aliases, uid_namespaces, overrides)


def _validate_override(slug, o: BuildOptions) -> None:
    try:
        o.validate()
    except ValueError as e:
        raise ValueError("jurisdictions.%s: %s" % (slug, e)) from None


def _check_meta(meta: SetMeta, names: Dict[str, str], present: set) -> None:
    """Re-validate a SetMeta handed straight to build_feedset (load_set_meta checks the same things)."""
    for slug, n in names.items():
        _check_name(slug, n)
    for slug, v in meta.short_names.items():
        _single_line(v, "jurisdictions.%s.short_name" % slug, MAX_SHORT_NAME)
    for slug, v in meta.statuses.items():
        if v not in STATUSES:
            raise ValueError("jurisdictions.%s.status must be one of: %s" % (slug, ", ".join(STATUSES)))
    _parse_aliases(meta.aliases)
    for kind, m in (("uid_namespace", meta.uid_namespaces), ("override", meta.overrides)):
        for slug in m:
            if not _is_slug(slug):
                raise ValueError("jurisdictions: key %r must be a slug" % (slug,))
            if slug not in present:
                raise ValueError("jurisdictions.%s has a %s but no records in this set (misspelt slug?)"
                                 % (slug, kind))
    for slug, ns in meta.uid_namespaces.items():
        _check_namespace(slug, ns)


def load_set_config(d) -> Tuple[BuildOptions, Dict[str, str]]:
    """Split a build-set config into (BuildOptions, {jurisdiction slug: display name}). Use load_set_meta to
    also get short names, statuses and aliases."""
    opts, names, _ = load_set_meta(d)
    return opts, names


def encode_index(index: dict) -> bytes:
    return (json.dumps(index, sort_keys=True, indent=1, ensure_ascii=False) + "\n").encode("utf-8")


def _field(r, name):
    return r.get(name) if isinstance(r, dict) else getattr(r, name, None)


def _apply_namespaces(records, namespaces: Dict[str, str]):
    """Records with the per-jurisdiction uid_namespace applied (inputs are not mutated). Dicts and
    MeetingRecords are both accepted, as by validate_records."""
    if not namespaces:
        return list(records)
    out = []
    for r in records:
        ns = namespaces.get(_field(r, "jurisdiction_slug"))
        if ns is None:
            out.append(r)
        elif isinstance(r, dict):
            out.append(dict(r, uid_namespace=ns))
        elif dataclasses.is_dataclass(r):
            out.append(replace(r, uid_namespace=ns))
        else:
            out.append(r)
    return out


def _check_cross_jurisdiction_uids(records) -> None:
    """A UID (namespace + record_id) may appear under one jurisdiction only: /mix/v1 merges feeds from different
    jurisdictions and relies on duplicates being impossible by construction."""
    seen: Dict[Tuple, str] = {}
    for r in records:
        ns, rid, j = _field(r, "uid_namespace"), _field(r, "record_id"), _field(r, "jurisdiction_slug")
        if not (isinstance(ns, str) and isinstance(rid, str) and isinstance(j, str)):
            continue  # validate_records reports it
        other = seen.setdefault((ns, rid), j)
        if other != j:
            raise ValueError("UID collision across jurisdictions: uid_namespace %r + record_id %r is used by both "
                             "'%s' and '%s'; give each jurisdiction its own uid_namespace in the set config"
                             % (ns, rid, other, j))


def prepare_records(records, meta: Optional[SetMeta] = None):
    """The set-level steps that precede record validation: apply the per-jurisdiction uid_namespace, then refuse
    a UID shared by two jurisdictions. Shared by build_feedset and `validate --config` so the pre-publish check
    cannot disagree with the build."""
    records = _apply_namespaces(list(records), meta.uid_namespaces if meta else {})
    _check_cross_jurisdiction_uids(records)
    return records


def _uid(r, opts):
    return "%s-%s@%s" % (r.uid_namespace, r.record_id, opts.uid_domain)


def _check_routing(records, opts):
    seen = {}
    names: Dict[Tuple[str, str], str] = {}
    conflicts: Dict[Tuple[str, str], set] = {}
    for r in records:
        uid = _uid(r, opts)
        for fld in ("jurisdiction_slug", "body_slug"):
            if not getattr(r, fld):
                raise ValueError("record %s: %s is required for build-set" % (uid, fld))
        if r.body_slug == COMBINED_SLUG:
            raise ValueError("record %s: body_slug 'all' is reserved for the combined feed" % uid)
        if uid in seen:
            raise ValueError("duplicate UID in input: %s" % uid)
        seen[uid] = True
        key = (r.jurisdiction_slug, r.body_slug)
        if key in names and names[key] != r.body:
            conflicts.setdefault(key, {names[key]}).add(r.body)
        names.setdefault(key, r.body)
    for (j, b), ns in sorted(conflicts.items()):
        n = sorted(ns)
        raise ValueError("body_slug '%s' in '%s' has conflicting body names: %s"
                         % (b, j, ", ".join("'%s'" % x for x in n)))


def _entry(f: FeedFile) -> dict:
    j, b = f.jurisdiction, f.body
    return {"body": b, "body_name": f.body_name, "cancelled": f.cancelled, "events": f.events,
            "href": "%s/%s.ics" % (j, b), "jurisdiction": j, "key": f.key, "kind": f.kind,
            "sha256": hashlib.sha256(f.data).hexdigest(), "size": len(f.data), "title": f.title}


def _check_aliases(aliases: dict, keys: set, jurisdictions: set) -> None:
    """Aliases are one step and never reuse a live slug: sources are not current, targets are, no chains."""
    for kind, current in (("bodies", {k[len("calendar/"):-len(".ics")] for k in keys}),
                          ("jurisdictions", jurisdictions)):
        m = aliases[kind]
        for src, dst in sorted(m.items()):
            if src in current:
                raise ValueError("aliases.%s: source %r is a current %s; an alias source must be a retired slug"
                                 % (kind, src, "feed" if kind == "bodies" else "jurisdiction"))
            if dst is None:
                continue
            if dst in m:
                raise ValueError("aliases.%s: target %r is itself an alias source (no chains)" % (kind, dst))
            if dst not in current:
                raise ValueError("aliases.%s: target %r is not in this set" % (kind, dst))


def build_feedset(records, options: Optional[BuildOptions] = None, ledger: Optional[Ledger] = None,
                  jurisdiction_names: Optional[Dict[str, str]] = None,
                  meta: Optional[SetMeta] = None) -> FeedSet:
    """Group records by (jurisdiction_slug, body_slug) and build every body feed, every combined feed and the
    index. All validation happens before any build, and every built feed is run through check.validate, so
    nothing malformed is returned. Raises RecordError for bad records and ValueError for the rest. The ledger
    passed in is never mutated; the updated ledger is FeedSet.ledger."""
    opts = options or BuildOptions()
    opts.validate()
    meta = meta or SetMeta({}, {}, _empty_aliases())
    names = jurisdiction_names or {}
    records = prepare_records(records, meta)
    records = validate_records(records)  # rejects empty input and every bad record
    _check_routing(records, opts)
    _check_meta(meta, names, {r.jurisdiction_slug for r in records})
    j_opts = {}
    for slug, ov in meta.overrides.items():
        j_opts[slug] = replace(opts, **ov)
        _validate_override(slug, j_opts[slug])
    work = Ledger(copy.deepcopy(ledger.entries) if ledger is not None else None)

    records = sorted(records, key=lambda r: (r.date, r.start_time, _uid(r, opts)))
    by_j: Dict[str, Dict[str, list]] = {}
    for r in records:
        by_j.setdefault(r.jurisdiction_slug, {}).setdefault(r.body_slug, []).append(r)

    feeds: List[FeedFile] = []
    bumped: List[str] = []
    for j in sorted(by_j):
        jn = names.get(j, j)
        jopts = j_opts.get(j, opts)
        for b in sorted(by_j[j]):
            recs = by_j[j][b]
            title = "%s: %s" % (jn, recs[0].body)
            data, rep = build_with_report(recs, replace(jopts, calendar_name=title), work)
            bumped += rep["bumped"]
            feeds.append(FeedFile("calendar/%s/%s.ics" % (j, b), data, "body", j, b, recs[0].body, title,
                                  rep["events"], rep["cancelled"]))
        everything = [r for r in records if r.jurisdiction_slug == j]
        title = "%s: All meetings" % jn
        data, rep = build_with_report(everything, replace(jopts, calendar_name=title), work)
        feeds.append(FeedFile("calendar/%s/%s.ics" % (j, COMBINED_SLUG), data, "combined", j, COMBINED_SLUG, None,
                              title, rep["events"], rep["cancelled"]))
    feeds.sort(key=lambda f: f.key)

    for f in feeds:
        errors, _ = check.validate(f.data, f.events)
        if errors:
            raise ValueError("built feed %s failed validation: %s" % (f.key, "; ".join(errors)))

    combined = [f for f in feeds if f.kind == "combined"]
    _check_aliases(meta.aliases, {f.key for f in feeds if f.kind == "body"}, set(by_j))
    jurisdictions = {}
    for j in sorted(by_j):
        full = names.get(j, j)
        if j not in meta.short_names and len(full) > MAX_SHORT_NAME:
            raise ValueError("jurisdictions.%s: name is over %d characters; set short_name" % (j, MAX_SHORT_NAME))
        jurisdictions[j] = {"name": full, "short_name": meta.short_names.get(j, full),
                            "status": meta.statuses.get(j, "live")}
    index = {"aliases": copy.deepcopy(meta.aliases),
             "feeds": [_entry(f) for f in feeds],
             "generated_at": max(r.retrieved_at for r in records),
             "jurisdictions": jurisdictions,
             "mix": dict(MIX),
             "schema": INDEX_SCHEMA,
             "totals": {"cancelled": sum(f.cancelled for f in combined), "events": sum(f.events for f in combined),
                        "feeds": len(feeds)}}
    return FeedSet(feeds, index, encode_index(index), work, sorted(set(bumped)))


def _inside(path, root):
    path, root = os.path.realpath(path), os.path.realpath(root)
    return path == root or path.startswith(root + os.sep)


def write_feedset(fs: FeedSet, out_dir: str, overwrite: bool = False) -> List[str]:
    """Write the feeds and index under <out_dir>/calendar and return the paths written. Refuses a symlinked
    calendar directory, any target that resolves outside out_dir, and a non-empty calendar directory unless
    overwrite is true (then only that subtree is replaced). Each file is written atomically."""
    cal = os.path.join(out_dir, "calendar")
    if os.path.islink(cal):
        raise ValueError("%s is a symlink; refusing" % cal)
    if os.path.isdir(cal) and os.listdir(cal):
        if not overwrite:
            raise ValueError("output directory %s is not empty; pass --overwrite to replace it" % cal)
        shutil.rmtree(cal)
    elif os.path.lexists(cal) and not os.path.isdir(cal):
        raise ValueError("%s exists and is not a directory" % cal)
    targets = [(os.path.join(out_dir, f.key), f.data) for f in fs.feeds]
    targets.append((os.path.join(out_dir, INDEX_KEY), fs.index_bytes))
    for path, _ in targets:  # check every target before writing any
        if not _inside(path, out_dir):
            raise ValueError("refusing to write outside output directory: %s" % path)
    written = []
    for path, data in targets:
        atomic_write(path, data)
        written.append(path)
    return written
