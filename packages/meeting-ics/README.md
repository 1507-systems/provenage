# meeting_ics (provenage-meeting-ics)

Generic civic meeting records to an RFC 5545 iCalendar feed. Part of Provenage. Python 3 standard library only, deterministic,
jurisdiction- and source-agnostic.

Purpose and boundary: this library builds the public calendar feed and nothing else. It has no RSVP,
attendance, ATTENDEE or member features, and never will: those belong to an install's member layer inside a
Provenage deployment, not to the public feed. Source-specific knowledge lives only in `adapters/`.

License: Apache-2.0 (see the repository root).

## Quickstart: from a spreadsheet to a subscribable calendar

You get a standard `.ics` calendar feed built from your own spreadsheet. No account, no network access while
building, no dependencies beyond Python 3.11 or later. Every name below is fictional; replace it with yours.

1. Install it into a virtual environment (or run `python3 -m meeting_ics` from this directory without installing):

        python3 -m venv .venv
        . .venv/bin/activate
        pip install "git+https://github.com/1507-systems/provenage#subdirectory=packages/meeting-ics"

2. Get the template, a file with only the header row, and fill in one row per meeting in a spreadsheet:

        python3 -m meeting_ics template --output meetings.csv

   Save it as CSV UTF-8 (in Excel: Save As > CSV UTF-8) and format the date and time columns as Text. Two rows, as
   they look in a text editor (`record_id` is yours to choose and must never change; `status` is one of `scheduled`,
   `rescheduled`, `cancelled`, `postponed`, `completed`):

        record_id,jurisdiction_slug,body_slug,body_name,meeting_type,date,start_time,end_time,timezone,status,location,join_url,dial_in,portal_url,agenda_url,packet_url,minutes_url,video_url,retrieved_at
        tc-101,my-town,town-council,Town Council,Regular Meeting,2027-01-12,18:30,,America/Chicago,scheduled,"Town Hall, 1 Main Street",,,https://my-town.example/meetings/tc-101,,,,,
        tc-102,my-town,town-council,Town Council,Special Meeting,2027-01-19,17:00,,America/Chicago,cancelled,"Town Hall, 1 Main Street",,,,,,,,

   `jurisdiction_slug` and `body_slug` (lowercase letters, digits, single hyphens) decide which feed a meeting goes
   to: one feed per body plus one combined feed, `all`, per jurisdiction. The columns are described under
   "CSV input" below.

3. Write `set.json`. `uid_domain` is a domain you control: it becomes part of every event's permanent identity,
   so choose it once. `jurisdictions` gives the feed titles.

        {"uid_domain": "calendar.my-town.example",
         "jurisdictions": {"my-town": {"name": "My Town"}}}

4. Check the file, then build. `--uid-namespace` is a short permanent prefix for your UIDs; `--retrieved-at now`
   stamps the build time. Rows you leave out are dropped from the feed, so cancel with `status`, never by deleting.

        python3 -m meeting_ics validate --adapter csv_template --uid-namespace my-town --retrieved-at now --input meetings.csv
        python3 -m meeting_ics build-set --adapter csv_template --uid-namespace my-town --retrieved-at now --input meetings.csv --config set.json --ledger ledger.json --out out

   This prints `feeds=2 events=2 cancelled=1 sequence_bumped=none` and writes `out/calendar/my-town/town-council.ics`,
   `out/calendar/my-town/all.ics` and `out/calendar/index.json`. Check a feed with
   `python3 -m meeting_ics check-feed out/calendar/my-town/all.ics`.

5. Publish the contents of `out/calendar/` on a web server over HTTPS with a publicly trusted certificate
   (see [docs/CALENDAR.md](../../docs/CALENDAR.md), "Transport" and "Subscription vs import"), then subscribe to
   `webcal://your-host/path/my-town/all.ics` in Apple Calendar, Outlook or Google Calendar ("from URL"). Subscribe
   to either the combined feed or a body feed, not both, or each meeting shows twice. Apple honours the feed's
   refresh hint, Outlook uses `X-PUBLISHED-TTL`, and Google refreshes on its own schedule (often many hours).

6. To change something, edit the spreadsheet, keep the same `record_id`, and rebuild with the same `--ledger`
   and `--overwrite` (the output folder is otherwise refused when it already holds feeds). Keep `ledger.json`
   safe: it remembers each meeting's SEQUENCE, which tells calendars a meeting changed. Losing it resets every
   SEQUENCE, and clients may then ignore your updates.

### Try the demo

The repository ships a fictional "Demo Town" with two bodies and eleven meetings, including a cancellation, a
reschedule, a postponement and meetings either side of the November daylight-saving change. From a checkout of
this repository, in `packages/meeting-ics`:

    python3 -m meeting_ics build-set --adapter csv_template --uid-namespace demo-town --retrieved-at 2026-10-01T15:00:00Z --input tests/fixtures/demo-town/meetings-v1.csv --config tests/fixtures/demo-town/options.json --ledger demo-ledger.json --out demo-out

Run the same command with `meetings-v2.csv`, `--retrieved-at 2026-11-20T15:00:00Z`, the same `--ledger` and
`--overwrite` to see the update: four meetings get SEQUENCE 1 and the rest are untouched. The expected outputs are
in `tests/fixtures/demo-town/expected/{v1,v2}/` and are compared byte for byte by `tests/test_demo_town.py`.
After an intended change to the builder, `python3 tests/regen_demo_town.py` rewrites them (review `git diff`);
`--check` reports differences without writing.

## Layout

- `model.py` MeetingRecord, the source-agnostic input record, and the status mapping
- `validate.py` the input contract: the only record validator
- `ical.py` escape, fold, VTIMEZONE generation
- `ledger.py` SEQUENCE ledger: strict load, atomic save, `monotonic_problems`
- `check.py` strict `.ics` parser and validator (`parse_ics`, `unescape_text`, `validate`), used by `check-feed`
- `build.py` `build(records, options) -> bytes`
- `feedset.py` `build_feedset(...)`: per-body feeds, a combined feed per jurisdiction and `index.json` (`build-set`)
- `cli.py` / `__main__.py` command line
- `adapters/primegov_normalized.py` the only PrimeGov-aware code
- `adapters/csv_template.py` the generic CSV adapter; `adapters/csv_template.csv` is its header row (`meeting_ics template`)
- `tests/` unittest suite; `tests/fixtures/demo-town/` is the fictional demo (CSV inputs, `options.json`, golden outputs)

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
first match wins; `x_prefix` (default `X-MEETING-ICS`; a host project may set its own, for example `X-EXAMPLE`); `calendar_name`; `calendar_description` (X-WR-CALDESC); `method` (`"PUBLISH"`, the default, or null to omit METHOD); `refresh_interval` (an RFC 5545 duration such as `PT1H`, the default, `P1D` or `PT30M`, or null to omit the refresh hints); `detail`: `links` (implemented),
`topics` and `full` (stubs that raise NotImplementedError, pending a design decision).
Options are validated before anything is built: a bad `uid_domain`, `x_prefix`, `prodid`, `method`,
`refresh_interval` or end-rule `basis` (letters, digits, `. _ -`, max 64) is a `ValueError`, and `--config` rejects
unknown keys so a misspelt or stray key fails instead of being ignored.
End precedence: record `end_time`, then end rule, then default duration. The end basis is always declared in
`<x_prefix>-DTEND-BASIS`.

### Calendar header and client behaviour

Header lines, in order: `BEGIN`, `VERSION`, `PRODID`, `CALSCALE`, `METHOD:PUBLISH` (if `method`), `X-WR-CALNAME`,
`X-WR-CALDESC`, `X-WR-TIMEZONE` (only when the feed has a single zone), `REFRESH-INTERVAL;VALUE=DURATION:<d>` and
`X-PUBLISHED-TTL:<d>` (if `refresh_interval`), then the VTIMEZONE blocks sorted by TZID. Times always carry a TZID
with a VTIMEZONE, never UTC alone.

Refresh hints are advisory: Apple Calendar honours `REFRESH-INTERVAL`, Outlook reads `X-PUBLISHED-TTL`, and Google
Calendar ignores both and refreshes on its own schedule (often many hours). `METHOD:PUBLISH` is the correct value
for a one-way public feed. None of these lines are hashed, so changing them never bumps SEQUENCE. Feeds built by
0.1.x gain the three lines once; pass `method=None, refresh_interval=None` (`"method": null, "refresh_interval":
null` in `--config`) to keep the old bytes.

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
12. Fold at 75 octets without splitting UTF-8, CRLF line ends, TEXT escapes for backslash, semicolon, comma, newline. Control characters other than tab are deleted from every escaped value, and the end-basis label is escaped too, so no text can add a line to the feed.
13. VTIMEZONE: built-in US-rule blocks (Denver, New_York, Chicago, Los_Angeles; Denver is exact), fixed zones
    (Phoenix, Honolulu, UTC), any other IANA zone derived from zoneinfo transitions for the years present. If
    zoneinfo or tz data is unavailable it raises RuntimeError.

## Use

    python3 -m meeting_ics build --input records.json --output feed.ics --ledger sequence.json --config opts.json

or, once installed, `meeting-ics build ...`. `--adapter primegov_normalized --uid-namespace NAME` reads a source file through an adapter instead; the namespace is required.

Check an input file without building: `python3 -m meeting_ics validate --input records.json` prints `ok records=N`
(exit 0) or one `<path>: <message>` line per problem and a summary (exit 2). All errors, from any command, exit 2.

Check a built feed: `python3 -m meeting_ics check-feed feed.ics [--expect-events N]` parses it strictly (CRLF,
75-octet lines, UTF-8, balanced components), then checks VERSION/PRODID, VTIMEZONE and VEVENT structure, unique
`local@domain` UIDs, UTC DTSTAMP and LAST-MODIFIED, local DTSTART/DTEND with a defined TZID, STATUS values, TEXT
escapes, `REFRESH-INTERVAL` parameters, and that no ATTENDEE, ORGANIZER or RSVP appears. It prints `FAIL: ...` and
`NOTE: ...` lines, then `<file>: events=N failures=M`, and exits 0 (clean), 1 (failures) or 2 (unreadable file).
It does not evaluate time-zone offsets; the tests cross-check those with the `icalendar` library.

`python3 -m meeting_ics template [--output PATH]` prints (or writes, refusing to overwrite) the header row of the
CSV input template (read it back with `--adapter csv_template`, below).

### CSV input (`--adapter csv_template`)

For a jurisdiction without a data feed, keep meetings in a spreadsheet and save it as CSV UTF-8:

    python3 -m meeting_ics template --output meetings.csv
    python3 -m meeting_ics build-set --adapter csv_template --uid-namespace NAME --input meetings.csv \
        --retrieved-at now --config set.json --ledger ledger.json --out out

`--adapter csv_template` works with `build`, `build-set` and `validate`. Columns: `record_id`, `jurisdiction_slug`,
`body_slug`, `body_name`, `meeting_type`, `date`, `start_time`, `end_time`, `timezone`, `status`, `location`,
`join_url`, `dial_in`, `portal_url`, `agenda_url`, `packet_url`, `minutes_url`, `video_url`, `retrieved_at`.
Required: `record_id`, `jurisdiction_slug`, `body_slug`, `body_name`, `meeting_type`, `date`, `start_time`,
`timezone`, `status`. Optional columns may be left out of the header.

- Header names are exact; a column starting with `#` is ignored, as is a row whose `record_id` starts with `#`
  and any blank row. Dates are `YYYY-MM-DD` (in Excel, format the column as Text). Times are 24-hour (`9:30` and
  `18:00:00` are accepted and normalized). `status` is case-insensitive and `canceled` is read as `cancelled`.
- `retrieved_at` (UTC `YYYY-MM-DDTHH:MM:SSZ`) is the DTSTAMP. Fill it per row, or pass `--retrieved-at TS|now`
  once; the cell wins. `--retrieved-at` is valid only with this adapter.
- `record_id` must never change or be reused: a reschedule keeps its id. To cancel, set `status` to `cancelled`;
  do not delete the row, or the event disappears from every subscriber's calendar.
- Problems are reported with the spreadsheet row, e.g. `row 3.date: must be a local date YYYY-MM-DD, got '12/1/26'`
  (`row 1` is the header). Every problem is listed in one pass and nothing is written.

`build` writes the `.ics` and then the ledger, each atomically (a `.tmp` file, then rename), and prints a warning
to stderr when `uid_domain` is still the placeholder `example.invalid`; UIDs are permanent once published.

### Feed sets (`build-set`)

    python3 -m meeting_ics build-set --input records.json --config set.json --ledger ledger.json --out out/

Every record needs `jurisdiction_slug` and `body_slug` (lowercase letters, digits and single hyphens; `all` is
reserved). The command writes `out/calendar/<jurisdiction>/<body>.ics` for each body, `out/calendar/<jurisdiction>/all.ics`
with every meeting of the jurisdiction, and `out/calendar/index.json` (schema in `docs/CALENDAR.md`). Meetings are
sorted by date, start time and UID, so the output does not depend on input order and a rerun is byte-identical.

`--config` takes the usual build options and must set `uid_domain`; it may also set
`"jurisdictions": {"demo-town": {"name": "Demo Town"}}`, which gives the feed titles `Demo Town: <body name>` and
`Demo Town: All meetings` (the slug is used when no name is given). `calendar_name` is not allowed. `--adapter` and
`--uid-namespace` work as for `build`. One ledger covers the whole set, so a meeting has the same UID and SEQUENCE in
its body feed and in `all.ics`; subscribing to both shows each meeting twice. Every built feed is run through
`check-feed` before anything is written; `out/calendar` must be empty unless you pass `--overwrite`, and the ledger
is saved last. Stdout is `feeds=N events=N cancelled=N sequence_bumped=<uids|none>`.

### SEQUENCE ledger

A ledger that is not valid (bad JSON, an empty file, a wrong top level, a malformed entry) is a `ValueError`
beginning `malformed ledger:`, never a fresh ledger, because starting over would reset every SEQUENCE. Only a
missing file starts fresh. `Ledger.loads(bytes)`, `.dumps()`, `.save(path)` (atomic) and
`monotonic_problems(old, new)` (UIDs missing from, or lower in, the new ledger) are available for publishers.

## Writing an adapter

Add `adapters/<name>.py` exposing `load(path) -> list[MeetingRecord]` (and ideally `convert(data)`). Do all
source knowledge there: unwrap source envelopes, choose the record_id, and take uid_namespace from the caller (the PrimeGov adapter requires it as a parameter, with no default), map the source's status
vocabulary onto the values above, map its URLs into `links`, flatten documents, take `retrieved_at` from the
fetch time. Put anything else the caller should keep in `provenance`. Nothing outside the adapter may mention the source.

## Tests

    cd packages/meeting-ics && python3 -m unittest discover -s tests -t .
    cd packages/meeting-ics && python3 -m pytest -q

`tests/test_icalendar_roundtrip.py` parses built feeds a second time with the `icalendar` library, which is a
test-only dependency pinned in the repository's `requirements-dev.txt`; those tests skip when it is not installed.
