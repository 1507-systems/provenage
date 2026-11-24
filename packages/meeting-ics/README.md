# meeting_ics (provenage-meeting-ics)

Generic civic meeting records to an RFC 5545 iCalendar feed. Part of Provenage. Python 3 standard library only, deterministic,
jurisdiction- and source-agnostic.

Purpose and boundary: this library builds the public calendar feed and nothing else. It has no RSVP,
attendance, ATTENDEE or member features, and never will: those belong to an install's member layer inside a
Provenage deployment, not to the public feed. Source-specific knowledge lives only in `adapters/`.

License: Apache-2.0 (see the repository root).

## Layout

- `model.py` MeetingRecord, the source-agnostic input record, and the status mapping
- `validate.py` the input contract: the only record validator
- `ical.py` escape, fold, VTIMEZONE generation
- `ledger.py` SEQUENCE ledger
- `build.py` `build(records, options) -> bytes`
- `cli.py` / `__main__.py` command line
- `adapters/primegov_normalized.py` the only PrimeGov-aware code
- `tests/` unittest suite

## Input schema

Input is a JSON array of records, or `{"records": [...], "schema_version": 1}`. It must contain at least one
record: an empty input is refused, never built into an empty feed. `meeting_ics validate --input FILE` checks a
file and prints every problem at once (`<path>: <message>`), so you can fix a file in one pass. `build` runs the
same checks first and writes nothing if any fail. The validator is `meeting_ics/validate.py`; this table is
documentation of it.

| Field | Type | Required | Rules |
| --- | --- | --- | --- |
| `uid_namespace` | string | yes | Letters, digits, `.` `_` `-`; starts with a letter or digit; max 128. Prefix of every UID |
| `record_id` | string or integer | yes | Stable id within the namespace. Integers (>= 0) become strings. Letters, digits and `. _ : + = / ~ -`; max 256. `namespace + record_id` must be unique in the input |
| `body_name` | string | one of `body_name` / `body` | Governing body, one line, max 200. `body` is the legacy name for the same field; give one, not both |
| `meeting_type` | string | yes | One line, max 200 |
| `date` | string | yes | Local date `YYYY-MM-DD`, a real date, year 1970 to 2100 |
| `start_time` | string | yes | Local 24-hour `HH:MM` |
| `end_time` | string or null | no | Observed local end `HH:MM`. Must differ from the start. An end earlier than the start means after midnight and is accepted only if the meeting lasts at most 12 hours |
| `end_basis` | string or null | no | Label for an observed end (default `source-observed`). Only together with `end_time`. Letters, digits, `. _ -`; max 64 |
| `timezone` | string | yes | IANA name that exists in tz data, such as `America/Denver` |
| `status` | string | yes | See the status table. Anything else is an error |
| `retrieved_at` | string | yes | UTC `YYYY-MM-DDTHH:MM:SSZ`; becomes DTSTAMP |
| `location` | string or null | no | Max 500, `\n` line breaks allowed |
| `remote_access` | object or null | no | `{"join_url": http(s) URL or null, "dial_in": string or null}`. Shown in DESCRIPTION and part of the change hash. `{}` and all-null count as null |
| `links` | object or null | no | `portal`, `agenda`, `packet`, `minutes`, `video`; each an http(s) URL (max 2048, with a host) or null |
| `documents` | array or null | no | `[{"kind": string, "url": URL or null, "publish_date": string or null}]`. Change hash only, never emitted |
| `topics` | array or null | no | Strings; reserved, not emitted |
| `provenance` | object or null | no | Free-form, carried, never inspected, emitted or hashed |
| `jurisdiction_slug` | string or null | no | Lowercase letters, digits and single hyphens, max 64. Routes the record to a published feed; never emitted or hashed |
| `body_slug` | string or null | no | Same rule; needs `jurisdiction_slug` |

Blank optional strings (empty or whitespace only) are treated as null; other strings are never trimmed. A record
that carried `""` in `location` or a link and was built by an older version gets one SEQUENCE bump after the
upgrade, because `""` and null hash differently. Unknown fields are errors, with a hint for near misses and for
link URLs given at the top level (`agenda_url` should be `links.agenda`).

Not validated: local times inside a DST gap or overlap (a time in the gap uses the offset before the gap), dates
in the past, and `uid_domain` and the other build options.

### Status

| Input status | STATUS | Extra line | SUMMARY |
| --- | --- | --- | --- |
| `scheduled` | CONFIRMED | none | unchanged |
| `confirmed` (alias) | CONFIRMED | none | unchanged |
| `scheduled_not_cancelled` (alias) | CONFIRMED | `<x_prefix>-STATUS-BASIS:derived-from-title-not-observed` | unchanged |
| `rescheduled` | CONFIRMED | `<x_prefix>-LIFECYCLE:rescheduled` | unchanged |
| `completed` | CONFIRMED | `<x_prefix>-LIFECYCLE:completed` | unchanged |
| `postponed` | TENTATIVE | `<x_prefix>-LIFECYCLE:postponed` | prefixed `Postponed: ` |
| `cancelled`, `canceled` (alias) | CANCELLED | none | unchanged; the event stays in the feed |

Statuses are lowercase. A record with a postponed meeting keeps its original date; if the source gives a new date,
send `rescheduled`. SEQUENCE is driven only by the change hash, which includes the raw status string and
`remote_access` when set.

## Options (`BuildOptions`, or `--config` JSON)

`prodid`; `uid_domain`; `default_duration` (JSON: `default_duration_hours` or `_minutes`); `end_rules`, a list of
(meeting_type substring, case-insensitive) to a fixed local end `"HH:MM"` or `{duration_minutes|end_local, basis}`,
first match wins; `x_prefix` (default `X-MEETING-ICS`; a host project may set its own, for example `X-EXAMPLE`); `calendar_name`; `detail`: `links` (implemented),
`topics` and `full` (stubs that raise NotImplementedError, pending a design decision).
End precedence: record `end_time`, then end rule, then default duration. The end basis is always declared in
`<x_prefix>-DTEND-BASIS`.

## Rules (the contract; as implemented in `build.py`)

0. Every record is validated first (see Input schema); any error stops the build before anything is written.
1. One VEVENT per record in input order; UID `<uid_namespace>-<record_id>@<uid_domain>`. Two records in one input with the same UID are an error (a `RecordError`, which is a `ValueError`).
2. DTSTAMP is `retrieved_at`, never the wall clock.
3. DTSTART/DTEND carry `TZID`; a VTIMEZONE is emitted for each zone present.
4. DTEND derived as above and always labelled as assumed, rule-set or observed.
5. SUMMARY is meeting_type, prefixed by body unless it already starts with it. Links level shows no agenda text.
6. DESCRIPTION is the labelled authoritative links (Portal, Agenda, Packet, Minutes, Video; null is omitted), then `Join online:` and `Dial-in:` from `remote_access` when present.
7. URL is the portal link; if absent, an AMBIGUITY note records it.
8. Status mapping as in the Status table.
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

Check an input file without building: `python3 -m meeting_ics validate --input records.json` prints `ok records=N`
(exit 0) or one `<path>: <message>` line per problem and a summary (exit 2). All errors, from any command, exit 2.

## Writing an adapter

Add `adapters/<name>.py` exposing `load(path) -> list[MeetingRecord]` (and ideally `convert(data)`). Do all
source knowledge there: unwrap source envelopes, choose the record_id, and take uid_namespace from the caller (the PrimeGov adapter requires it as a parameter, with no default), map the source's status
vocabulary onto the values above, map its URLs into `links`, flatten documents, take `retrieved_at` from the
fetch time. Put anything else the caller should keep in `provenance`. Nothing outside the adapter may mention the source.

## Tests

    cd packages/meeting-ics && python3 -m unittest discover -s tests -t .
    cd packages/meeting-ics && python3 -m pytest -q
