# Calendar

## Rules

- One VEVENT per meeting.
- The UID is stable and survives revisions (rescheduling, renaming, edits).
- Cancellation is an update with `STATUS:CANCELLED`, never a deletion.
- SEQUENCE is bumped on a calendar-critical change or a change to the document set
  (see the ledger below).
- Each feed carries a VTIMEZONE for its jurisdiction.
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
