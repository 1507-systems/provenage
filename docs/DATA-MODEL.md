# Data model (initial field contract)

Status: initial. Changes get stricter review (see CONTRIBUTING.md). Fields marked
**CC** are calendar-critical: a change bumps the SEQUENCE for that meeting.

Every asserted value below also carries the provenance envelope (source id, source
class, source record id, source URL, retrieved_at, content hash, derivation).

## Identity

| Field | Notes | CC |
| --- | --- | --- |
| jurisdiction | Stable slug, for example `longmont-co` | yes |
| body | Stable slug within the jurisdiction, for example `city-council` | yes |
| meeting id | Stable id within the body; the basis of the calendar UID | yes |

## Schedule

| Field | Notes | CC |
| --- | --- | --- |
| date | Local calendar date | yes |
| start_time | Local time | yes |
| timezone | IANA name, per jurisdiction | yes |
| status | scheduled, rescheduled, cancelled, postponed, completed | yes |

Lifecycle states: `scheduled`, `rescheduled`, `cancelled`, `postponed`, `completed`.

## Location and remote access

| Field | Notes | CC |
| --- | --- | --- |
| location | Name and address | yes |
| remote access | Video or phone join details as published | yes |

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
