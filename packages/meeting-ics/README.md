# meeting_ics (provenage-meeting-ics)

Generic civic meeting records to an RFC 5545 iCalendar feed. Part of Provenage. Python 3 standard library only, deterministic,
jurisdiction- and source-agnostic.

Purpose and boundary: this library builds the public calendar feed and nothing else. It has no RSVP,
attendance, ATTENDEE or member features, and never will: those belong to an install's member layer inside a
Provenage deployment, not to the public feed. Source-specific knowledge lives only in `adapters/`.

License: Apache-2.0 (see the repository root).

## Layout

- `model.py` MeetingRecord, the source-agnostic input record
- `ical.py` escape, fold, VTIMEZONE generation
- `ledger.py` SEQUENCE ledger
- `build.py` `build(records, options) -> bytes`
- `cli.py` / `__main__.py` command line
- `adapters/primegov_normalized.py` the only PrimeGov-aware code
- `tests/` unittest suite

## Input schema (one record)

`uid_namespace`, `record_id`, `body`, `meeting_type`, `date` (YYYY-MM-DD local), `start_time` (HH:MM local),
`timezone` (IANA), `status`, `retrieved_at` (UTC `YYYY-MM-DDTHH:MM:SSZ`), and optional `location`,
`links` {portal, agenda, packet, minutes, video}, `documents` [{kind, url, publish_date}], `end_time`
(observed local end), `end_basis`, `topics` (reserved), `provenance` (free-form, carried, never emitted or hashed).
Status values: `cancelled` (STATUS:CANCELLED, kept), `confirmed` (CONFIRMED, observed),
`scheduled_not_cancelled` (CONFIRMED plus a STATUS-BASIS note, derived not observed); anything else gets no
STATUS and an AMBIGUITY note. Full field docs are in `model.py`.

## Options (`BuildOptions`, or `--config` JSON)

`prodid`; `uid_domain`; `default_duration` (JSON: `default_duration_hours` or `_minutes`); `end_rules`, a list of
(meeting_type substring, case-insensitive) to a fixed local end `"HH:MM"` or `{duration_minutes|end_local, basis}`,
first match wins; `x_prefix` (default `X-MEETING-ICS`; a host project may set its own, for example `X-EXAMPLE`); `calendar_name`; `detail`: `links` (implemented),
`topics` and `full` (stubs that raise NotImplementedError, pending a design decision).
End precedence: record `end_time`, then end rule, then default duration. The end basis is always declared in
`<x_prefix>-DTEND-BASIS`.

## Rules (the contract; as implemented in `build.py`)

1. One VEVENT per record in input order; UID `<uid_namespace>-<record_id>@<uid_domain>`. Two records in one input with the same UID are an error (`ValueError`).
2. DTSTAMP is `retrieved_at`, never the wall clock.
3. DTSTART/DTEND carry `TZID`; a VTIMEZONE is emitted for each zone present.
4. DTEND derived as above and always labelled as assumed, rule-set or observed.
5. SUMMARY is meeting_type, prefixed by body unless it already starts with it. Links level shows no agenda text.
6. DESCRIPTION is labelled authoritative links only (Portal, Agenda, Packet, Minutes, Video); null is omitted.
7. URL is the portal link; if absent, an AMBIGUITY note records it.
8. Status mapping as above.
9. LOCATION when present, else AMBIGUITY `no-location`.
10. SEQUENCE from the ledger, keyed by UID, storing critical_hash, docs_hash, sequence, last_modified. First sight
    is 0; if the critical hash (body, date, start_time, timezone, status, location, meeting_type, derived end and
    its basis) or the docs hash (links and sorted documents) changes, SEQUENCE increments and last_modified becomes
    this run's DTSTAMP. UIDs absent from input stay in the ledger.
11. LAST-MODIFIED is the ledger last_modified.
12. Fold at 75 octets without splitting UTF-8, CRLF line ends, TEXT escapes for backslash, semicolon, comma, newline.
13. VTIMEZONE: built-in US-rule blocks (Denver, New_York, Chicago, Los_Angeles; Denver is exact), fixed zones
    (Phoenix, Honolulu, UTC), any other IANA zone derived from zoneinfo transitions for the years present. If
    zoneinfo or tz data is unavailable it raises RuntimeError.

## Use

    python3 -m meeting_ics build --input records.json --output feed.ics --ledger sequence.json --config opts.json

or, once installed, `meeting-ics build ...`. `--adapter primegov_normalized --uid-namespace NAME` reads a source file through an adapter instead; the namespace is required.

## Writing an adapter

Add `adapters/<name>.py` exposing `load(path) -> list[MeetingRecord]` (and ideally `convert(data)`). Do all
source knowledge there: unwrap source envelopes, choose the record_id, and take uid_namespace from the caller (the PrimeGov adapter requires it as a parameter, with no default), map the source's status
vocabulary onto the values above, map its URLs into `links`, flatten documents, take `retrieved_at` from the
fetch time. Put anything else the caller should keep in `provenance`. Nothing outside the adapter may mention the source.

## Tests

    cd packages/meeting-ics && python3 -m unittest discover -s tests -t .
    cd packages/meeting-ics && python3 -m pytest -q
