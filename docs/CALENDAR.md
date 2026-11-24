# Calendar

## Rules

- One VEVENT per meeting.
- The UID is stable and survives revisions (rescheduling, renaming, edits).
- Cancellation is an update with `STATUS:CANCELLED`, never a deletion.
- Status maps to STATUS as follows: scheduled, rescheduled and completed are `CONFIRMED`; postponed is
  `TENTATIVE` and its SUMMARY is prefixed `Postponed: `; cancelled is `CANCELLED`. Rescheduled, completed and
  postponed also carry `<x_prefix>-LIFECYCLE:<state>` (for example `X-MEETING-ICS-LIFECYCLE:postponed`), because
  clients do not render those states from STATUS alone. A status derived from the title rather than observed
  carries `<x_prefix>-STATUS-BASIS:derived-from-title-not-observed`. See docs/DATA-MODEL.md for the full table.
- The builder validates every input record before building and refuses an empty input rather than
  publishing an empty feed. An unknown status is an error, not a silent omission of STATUS.
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

Use `webcal://` to subscribe. Example (not yet live):

    webcal://api.1507.systems/provenage/calendar/longmont-co/city-council.ics

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
3. Default duration.

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
