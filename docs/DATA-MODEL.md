# Data model (initial field contract)

Status: initial. Changes get stricter review (see CONTRIBUTING.md). Fields marked
**CC** are calendar-critical: a change bumps the SEQUENCE for that meeting. The one exception is the
routing slugs (jurisdiction and body below): they decide which published feed a meeting appears in, never what
the event says, so they are not hashed and moving a meeting to another feed is not a change to the event.

Every asserted value below also carries the provenance envelope (source id, source
class, source record id, source URL, retrieved_at, content hash, derivation).

## Identity

| Field | Notes | CC |
| --- | --- | --- |
| jurisdiction | Stable slug, for example `longmont-co` (`jurisdiction_slug` in the builder input) | no, routing only |
| body | Stable slug within the jurisdiction, for example `city-council` (`body_slug`; needs the jurisdiction slug) | no, routing only |
| meeting id | Stable id within the body; the basis of the calendar UID | yes |

## Schedule

| Field | Notes | CC |
| --- | --- | --- |
| date | Local calendar date | yes |
| start_time | Local time | yes |
| timezone | IANA name, per jurisdiction | yes |
| status | scheduled, rescheduled, cancelled, postponed, completed | yes |

Lifecycle states: `scheduled`, `rescheduled`, `cancelled`, `postponed`, `completed`. The calendar builder
also accepts the aliases `confirmed`, `scheduled_not_cancelled` (derived from the title, not observed) and
`canceled`. Any other value is a hard error. A postponed meeting keeps its original date; if the source gives a
new date, the state is `rescheduled`.

Mapping to the calendar feed (`<X>` is the configured property prefix, `X-MEETING-ICS` by default):

| Status | iCalendar STATUS | Extra property | SUMMARY |
| --- | --- | --- | --- |
| scheduled, confirmed | CONFIRMED | none | unchanged |
| scheduled_not_cancelled | CONFIRMED | `<X>-STATUS-BASIS:derived-from-title-not-observed` | unchanged |
| rescheduled | CONFIRMED | `<X>-LIFECYCLE:rescheduled` | unchanged |
| completed | CONFIRMED | `<X>-LIFECYCLE:completed` | unchanged |
| postponed | TENTATIVE | `<X>-LIFECYCLE:postponed` | prefixed `Postponed: ` |
| cancelled, canceled | CANCELLED | none | unchanged, event kept |

The SEQUENCE bump comes only from the change hash (which includes the raw status string); there is no separate
status-driven bump.

## Location and remote access

| Field | Notes | CC |
| --- | --- | --- |
| location | Name and address | yes |
| remote access | Video or phone join details as published. In the builder input: `remote_access` = `{"join_url": URL or null, "dial_in": text or null}`; shown in the event DESCRIPTION as `Join online:` and `Dial-in:` | yes |

## Documents

Each document has a kind, URL, publish timestamp and content hash.

| Kind | Notes |
| --- | --- |
| agenda | Publish timestamp recorded |
| packet | Publish timestamp recorded |
| minutes | Publish timestamp recorded |
| video | Link to recording |
| transcript | Publish timestamp recorded |

Adding or removing a document changes the document set and bumps SEQUENCE.

## Agenda hierarchy

Items form a tree (sections contain items, items may contain sub-items). Per item:

| Field | Notes |
| --- | --- |
| number | As printed, for example `7.b` |
| title | |
| description | |
| type | For example presentation, consent, public hearing, ordinance, resolution |
| attachments | Documents attached to the item |

## Outcomes

| Field | Notes |
| --- | --- |
| motions | Text, mover, seconder if published |
| votes | Per-member or tally, as published |
| ordinances | Number, title, disposition |
| resolutions | Number, title, disposition |

Outcomes are not calendar-critical.
