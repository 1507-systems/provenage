# Calendar

## Rules

- One VEVENT per meeting.
- The UID is stable and survives revisions (rescheduling, renaming, edits).
- Cancellation is an update with `STATUS:CANCELLED`, never a deletion.
- Status maps to STATUS as follows: scheduled, rescheduled and completed are `CONFIRMED`; postponed is
  `TENTATIVE` and its SUMMARY is prefixed `Postponed: `; cancelled is `CANCELLED` and its SUMMARY is prefixed `Cancelled: ` (some clients show a
  cancelled event in a subscribed calendar like a normal one; the prefix is not doubled when the source title already
  starts with it). The prefix is not hashed, so adding it does not bump SEQUENCE. Rescheduled, completed and
  postponed also carry `<x_prefix>-LIFECYCLE:<state>` (for example `X-MEETING-ICS-LIFECYCLE:postponed`), because
  clients do not render those states from STATUS alone. A status derived from the title rather than observed
  carries `<x_prefix>-STATUS-BASIS:derived-from-title-not-observed`. See docs/DATA-MODEL.md for the full table.
- The builder validates every input record before building and refuses an empty input rather than
  publishing an empty feed. An unknown status is an error, not a silent omission of STATUS.
- The first link in DESCRIPTION is labelled `Meeting page:` (the record's `links.portal`, which is the city's page for
  that meeting, not necessarily a portal). The `URL` property carries the same link.
- Join details (`remote_access`) appear in DESCRIPTION after the links, as `Join online: <url>` and
  `Dial-in: <text>`, and a change to them bumps SEQUENCE.
- SEQUENCE is bumped on a calendar-critical change or a change to the document set
  (see the ledger below).
- Each feed carries a VTIMEZONE for its jurisdiction.
- Each feed declares `METHOD:PUBLISH` and a refresh hint (`REFRESH-INTERVAL;VALUE=DURATION:PT1H` plus
  `X-PUBLISHED-TTL:PT1H`). Apple honours the first, Outlook the second, Google neither. They are not hashed.
- Every built feed can be checked with `meeting_ics check-feed FILE` (strict RFC 5545 structure, TEXT escapes,
  no ATTENDEE, ORGANIZER or RSVP).
- Combined feeds keep body identity in the title prefix: the builder prefixes the
  meeting type with the body name unless it already starts with it, for example
  `City Council Regular Session`.
- No ATTENDEE, RSVP or ORGANIZER, ever. See Boundary.

## URL scheme

    https://api.1507.systems/provenage/calendar/<jurisdiction>/<body>.ics

Use `webcal://` to subscribe. Example (illustrative; check the feed index for what is published):

    webcal://api.1507.systems/provenage/calendar/longmont-co/city-council.ics

### Slugs

A slug is lowercase letters and digits with single hyphens (`^[a-z0-9]+(-[a-z0-9]+)*$`, at most 64 characters).
`all` is reserved: it names a jurisdiction's combined feed, and a record whose body slug is `all` is refused.

The convention for a jurisdiction slug is `<place>-<2-letter state>`, for a city, town, village or county alike:
`longmont-co`, `boulder-county-co`. (Counties keep the word `county` in the place part.) Fictional test places use the
non-existent state code `xx`, for example `alder-falls-xx`. Body slugs name the body, for example `city-council`.
A slug is part of every address issued under it, so choose it once. The set config can retire or rename one only by
recording an alias (see "Aliases").

## Feed index

`meeting_ics build-set` writes the feeds and `calendar/index.json` (schema `provenage-calendar-index/1`). Per
jurisdiction there is one feed per body and one combined `all.ics`. A meeting has the same UID, SEQUENCE and event
text in both, so subscribe to one or the other, not both.

    {"feeds": [{"body": "all", "body_name": null, "cancelled": 1, "events": 3,
                "href": "demo-town/all.ics", "jurisdiction": "demo-town", "key": "calendar/demo-town/all.ics",
                "kind": "combined", "sha256": "<64 hex>", "size": 1234, "title": "Demo Town: All meetings"}],
     "generated_at": "2026-03-05T06:00:00Z", "schema": "provenage-calendar-index/1",
     "totals": {"cancelled": 1, "events": 3, "feeds": 3}}

- `feeds` is sorted by `key`. `kind` is `body` or `combined`. `body` is the body slug (`all` for combined) and
  `body_name` the display name (null for combined). `title` is the feed's `X-WR-CALNAME`.
- `href` is relative to the index URL; `key` is the storage key. `sha256` and `size` describe the exact feed bytes.
- `events` and `cancelled` count VEVENTs in that feed. `totals` sums the combined feeds only, so each meeting is
  counted once; `totals.feeds` counts every feed.
- `generated_at` is the latest `retrieved_at` among the records, never the wall clock.
- The file is `json.dumps(sort_keys=True, indent=1, ensure_ascii=False)` plus a newline, in UTF-8.
- The SEQUENCE ledger is not part of this tree and is never served. It lives at `state/ledger.json` in the same
  bucket, with a one-version backup at `state/ledger.prev.json`; the Worker has no route for `state/`. Publishing is
  described in `workers/feeds/README.md`.

Three more top-level keys were added to the same schema version (all additive):

    {"jurisdictions": {"demo-town": {"name": "Demo Town", "short_name": "Demo Town", "status": "dev"}},
     "aliases": {"bodies": {"longmont-co/planning-commission": "longmont-co/planning-and-zoning-commission",
                            "longmont-co/old-task-force": null},
                 "jurisdictions": {}},
     "mix": {"issue_max_bytes": 2000000, "max_refs": 32, "max_selection_bytes": 1536,
             "path": "mix/v1/", "version": 1}}

- `jurisdictions` has one entry per jurisdiction in the set. `name` is the full display name; `short_name` (at most
  40 characters, default `name`) is what the place prefix and the merged calendar's name use; `status` is `live`
  (default) or `dev`. A `dev` jurisdiction is test data and carries no permanence promise (see "Permanence").
- `aliases` records slugs that were renamed (`"old": "new"`) or retired (`"old": null`). Body aliases are keyed
  `<jurisdiction>/<body>`, jurisdiction aliases by slug. Aliases are one step (a target is never itself an alias
  source), and the publisher refuses to remove or change one (see `workers/feeds/README.md`).
- `mix` publishes the limits of the combined-address route so the builder page reads them from data. The page uses
  `max_refs` and `issue_max_bytes` (it will not issue an address whose selected feeds total more than that many
  bytes). The Worker enforces the first two itself as constants of the same value (32 and 1,536); the size limit is
  applied by the page only.

**Consumers ignore unknown fields.** The index may gain keys without a schema change; a reader must not fail on
a key it does not know.

## Combined addresses (`/mix/v1/`)

One address can carry any selection of published bodies, so a person subscribes once instead of once per body. The
builder page (an early preview, published at https://1507-systems.github.io/provenage/) issues these addresses. At
this stage the real meetings are an unofficial sample for Longmont, Colorado; places in the fictional state `xx` are
synthetic test data for trying the builder.

    https://api.1507.systems/provenage/mix/v1/<selection>.ics
    webcal://api.1507.systems/provenage/mix/v1/<selection>.ics

### Grammar

    selection = group *( "_" group )
    group     = slug "." member
    member    = "all" / body *( "." body )

`.` and `_` never occur in a slug, and `all` is reserved, so the parse is unambiguous. `<jurisdiction>.all` is that
jurisdiction's combined feed and includes bodies added later. Examples:

    /provenage/mix/v1/longmont-co.city-council.ics
    /provenage/mix/v1/longmont-co.city-council.planning-and-zoning-commission.ics
    /provenage/mix/v1/alder-falls-xx.all_longmont-co.city-council.ics

### Canonical form and limits

The address must already be canonical; the Worker does not redirect. It is checked on the literal string, before any
alias is applied:

1. groups in ascending byte order of jurisdiction slug, each jurisdiction once;
2. bodies within a group in ascending byte order, none repeated;
3. `all` only as the sole member of its group;
4. lowercase ASCII only, no percent-encoding, no empty segment, no trailing slash.

At most **32** references (each body counts 1, each `all` counts 1) and at most **1,536** bytes of `<selection>`.
The length is checked first, before parsing. The query string is ignored for content, ETag and caching.

Any grammar, canonical-form or limit failure is `404` with JSON `{"error": "..."}` and `Cache-Control: no-store`. When
the only problem is order or repeats, the body also carries `"canonical": "/provenage/mix/v1/<fixed>.ics"`. A
jurisdiction given once as `all` and once with bodies has no unambiguous fix and gets no `canonical`.

### Responses

| Status | When |
|---|---|
| 200 | The merged calendar. `Content-Type: text/calendar; charset=utf-8`, `ETag: "m1-<32 hex>"`, `Last-Modified` (when the index object was uploaded), `Cache-Control: public, max-age=300`. `X-Provenage-Missing: <n>` appears only when n > 0. HEAD returns the same headers without a body. |
| 304 | `If-None-Match` matches (or, without it, `If-Modified-Since` is not older than the index upload). No source feed is read. |
| 404 | Bad address (above), or every selected calendar is retired or missing. |
| 503 | The index is unavailable (and no cached copy under an hour old), a source feed cannot be read, is not laid out as the builder writes it, or does not match its index `sha256`, or the merged output would exceed 8 MiB. `Retry-After: 300` (`60` for a `sha256` mismatch), `Cache-Control: no-store`. |

A failure is never an empty or partial calendar: subscribed clients delete events missing from a 200 but keep their
last copy on an error. The ETag is derived from the index and the address alone.

### What the merged calendar contains

- Each source feed is read from the bucket and checked against the `sha256` in the index. Every VEVENT is copied
  byte for byte, so UID, SEQUENCE and the rest are exactly those of the source feed. The only change is the place
  prefix below.
- The header is generated: `METHOD:PUBLISH`, `PRODID:-//Provenage//mix v1//EN`, `REFRESH-INTERVAL;VALUE=DURATION:PT1H`
  and `X-PUBLISHED-TTL:PT1H`, `X-WR-TIMEZONE` only when every source uses the same single time zone, and one
  VTIMEZONE per zone (identical blocks merge; if two differ, the longer is kept).
- Name (`X-WR-CALNAME`): one selected feed uses that feed's title. Several feeds in one jurisdiction:
  `<short_name>: <body name>, <body name>` when that is at most 60 characters, otherwise
  `<short_name>: <first body name> + <k> more`. Several jurisdictions: `Local meetings: <short_name>, <short_name>`
  when at most 60 characters, otherwise `Local meetings: <n> places`. A combined feed counts as `All meetings`.
  There is no user-chosen name.
- Description: `Unofficial merged calendar from Provenage. Change this selection: <page address>#v1=<selection>`, plus
  ` <n> selected calendars are no longer published.` when some were retired or missing.
- Text taken from the index (names, short names) has control characters removed, `\`, `;` and `,` escaped, the name
  capped at 120 characters and the description at 1,000 octets, and lines folded at 75 octets without splitting a
  UTF-8 character, so an odd name cannot inject a property.
- **Place prefix.** When the selected feeds span more than one jurisdiction, every event's SUMMARY gets
  `<short_name>: ` in front (for example `Longmont: City Council Regular Session`), so two places' "City Council"
  meetings are distinguishable. With a single jurisdiction there is no prefix. The prefix is a function of the
  address, so it never changes SEQUENCE.
- **No duplicates by construction.** After alias resolution a repeated feed is kept once, and a body feed is dropped
  when its jurisdiction's `all` feed is also selected. A meeting has one UID, held in one body feed and that
  jurisdiction's `all` feed, so a merged calendar never carries a UID twice. `build-set` refuses a UID shared by two
  jurisdictions.

### Aliases

Selections are resolved against the index's `aliases` one step at a time: a body alias (`<j>/<b>`) wins over a
jurisdiction alias; a jurisdiction alias applies to `<j>.all` and to every body of `<j>`; a `null` target means
retired. A retired or missing reference is skipped and counted in `X-Provenage-Missing` and the description. An
issued address therefore keeps working through renames. Direct feed addresses (`/calendar/<j>/<b>.ics`) are not
rewritten by aliases: a renamed body's old direct address keeps serving its last published feed, which no longer
updates.

### What v1 freezes

The grammar, the canonical rules, the limits a v1 address may use, alias resolution, the set of events a selection
yields and the place-prefix rule. Not frozen: calendar name and description wording, refresh hints and header
order. A change to anything frozen is a `v2` served beside `v1`.

### Permanence

Issued `/mix/v1/` addresses keep working for as long as Provenage serves feeds. The publisher refuses (exit 4,
overridable only by an explicit `--force`) changes that would break one: an alias, once recorded, is never removed or
changed, a feed in a non-`dev` jurisdiction cannot disappear from the index unless an alias covers it, and a live jurisdiction cannot be switched to `dev` without that same explicit override. Marking a live jurisdiction `dev` is a maintainer decision that withdraws this promise for it; it is not done for a jurisdiction with issued addresses.

Exemption: a jurisdiction with `status: "dev"` (test data, such as a slug kept only for demonstration) carries no
promise. Its feeds may be removed or replaced with different data by the publisher (removing a whole one needs a confirmed `--force` run), and the builder page labels them "Test data". Do not rely on an address that names one. Fictional `xx` places are test data for the same reason.

### Privacy

The Worker stores no selection and keeps no log: observability is off in its configuration, its source calls no
`console.*` (a test fails if it does), and there is no per-request storage. What remains outside its control:
Cloudflare may see request paths in its own analytics; GitHub sees page loads of the builder page, but never the
selection, because it lives in the URL fragment, which browsers do not send; and a subscriber's calendar provider
sees the address they gave it. The `.ics` and `/mix/` responses carry no CORS header; only `calendar/index.json` does,
so the builder page on another origin can read it.

## Subscription vs import

Delivery is subscription. A subscribed client re-fetches and applies updates by
UID and SEQUENCE. File import is one-shot: the client copies events once and never
refreshes them. Apple Calendar import does not replace an existing UID, so
re-importing a changed file creates duplicates or keeps the old event. Prefer
subscribing.

## Transport

Feeds must be served over HTTPS with a publicly trusted certificate. Apple
Calendar's subscription fetcher rewrites `webcal://` to `https://` and never
falls back to plain HTTP: against a plain-HTTP server it sends only TLS
handshakes and reports that the request failed, even though the same URL opens
in a browser and the subscription sheet's preview loaded it. A subscription
stored in iCloud is fetched by Apple's servers, not by the subscriber's machine,
so the feed must also be reachable from the public internet. For local testing,
put a tunnel with a trusted certificate in front of the development server.

## Detail levels

- links only (default): title, time, place, status and links to authoritative
  records and documents.
- topics-only (planned): adds agenda item titles.
- full-structure (planned): adds the agenda hierarchy.

## End-time policy

Sources rarely publish an end time. In order:

1. Observed end, if the source gives one.
2. Per-meeting-type rule, for example a duration configured for study sessions.
3. Default duration: 3 hours unless the feed sets `default_duration` (for example `"PT90M"`). Set it shorter where a
   meeting with no published end would otherwise overlap the one that follows it.

The basis used is always declared in an X- property on the event,
`<x_prefix>-DTEND-BASIS` (`X-MEETING-ICS-DTEND-BASIS` by default; an install may set
its own prefix, for example `X-PROVENAGE`). Values name the basis: `source-observed`
for a published end, `rule-end-HHMM-local` or a configured label for a
per-type rule, and `default-duration-3h-assumed` (or the configured duration) for
the default. A consumer never mistakes an estimate for a published end time.

## SEQUENCE ledger

A ledger records, per UID, the last SEQUENCE and a hash of the calendar-critical
fields and document set. On each build, if the hash differs, SEQUENCE is
incremented and the ledger updated; otherwise SEQUENCE is unchanged so output is
byte-stable. Calendar-critical fields are marked in [DATA-MODEL.md](DATA-MODEL.md).

## Boundary

The public calendar is identical for every subscriber. It never contains ATTENDEE,
RSVP or ORGANIZER properties, attendance, or per-person flags. Member features
(layer 3) are delivered as a separate shared calendar inside an install, behind
accounts.
