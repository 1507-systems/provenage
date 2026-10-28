# Architecture

Status: this describes the target design. Today only layer 1
(`packages/meeting-ics`) and the feed Worker exist; the evidence store,
normalization, graph, API, exports and member layer are not built yet.

## Principles

1. The government is the system of record. Provenage reports what a source
   published and when, and links back to it.
2. Every asserted value carries its provenance.
3. Prefer authoritative records, preserve disagreement.
4. Deployment target is not architecture. Cloudflare is where it runs first.

## The three layers

**Layer 1: generic calendar builder** (`packages/meeting-ics`). A library that turns
a list of meeting dicts into iCalendar. Source-agnostic, no accounts, no network,
Python standard library only. It never emits ATTENDEE, RSVP or ORGANIZER data. It
can be used without the rest of Provenage.

**Layer 2: a Provenage install.** A deployed instance containing adapters, an
evidence store, normalization, the graph, the API, bulk exports, and canonical
per-body feeds (iCalendar, RSS/Atom). Public reads need no account.

**Layer 3: member layer.** RSVP, attendance, watch lists and topic flags. It exists
only inside an install, behind accounts. Members receive it as a shared calendar.

### Why the member layer is never in the public feed

Public feeds are cached, mirrored and subscribed to by people we cannot identify.
Putting any per-person state in them would leak it and would also make the feed
non-canonical (different bytes for different people). The public feed is the same
for everyone and contains only what the sources published. Member data is private
by construction and is delivered through a separate, authenticated path.

## Pipeline

    acquisition -> evidence -> normalization -> graph -> outputs

- **Acquisition.** Adapters fetch from a source under its access/legal gate and
  record a retrieval attestation.
- **Evidence.** The retrieved bytes (when rights allow) and metadata are stored,
  addressed by content hash. Nothing downstream may exist without evidence.
- **Normalization.** Deterministic parsing maps evidence into the typed model in
  [DATA-MODEL.md](DATA-MODEL.md). Same evidence in, same output out.
- **Graph.** Typed entities (jurisdiction, body, meeting, agenda item, document,
  outcome) with provenance-bearing assertions and their history.
- **Outputs.** API, bulk exports, RSS/Atom, iCalendar.

## Provenance envelope

Attached to every asserted value:

| Field | Meaning |
| --- | --- |
| source id | Which configured source produced it |
| source class | One of the classes below |
| source record id | The source's own identifier for the record |
| source URL | Where it was retrieved |
| retrieved_at | UTC timestamp of retrieval |
| content hash | Hash of the retrieved bytes |
| derivation | How the value was obtained from the evidence (direct, parsed, inferred, with the rule) |

## Source classes

1. government-primary: the originating government's own systems.
2. government-derived aggregator: a vendor or aggregator republishing government
   records.
3. journalism.
4. other secondary.
5. derived/inferred: computed by Provenage or a model from other values.

Rule: prefer authoritative records (class 1 over 2 over the rest). When sources
disagree, the preferred value is shown but the others are retained with their
envelopes so the disagreement stays visible. Aggregators are optional cross-check
adapters, each behind the access/legal gate. Mirror exact bytes only when rights
allow; otherwise keep metadata, hash and the authoritative link.

## Change detection

Each retrieval is compared with the previous one by content hash, then by
normalized field. A change record states what changed, the old and new values, and
both envelopes. Calendar-critical changes (see [DATA-MODEL.md](DATA-MODEL.md))
bump the iCalendar SEQUENCE for that meeting; see [CALENDAR.md](CALENDAR.md).

## Portability

Cloudflare Workers and R2 are the first deployment target. Components talk to
storage and scheduling through narrow interfaces so an install can run elsewhere.
No layer-1 code depends on Cloudflare.

## What an install is

One deployed instance of layer 2, operated by someone: a set of enabled adapters
and jurisdictions, an evidence store, a graph, and the public outputs. The
maintainer operates the first install on a shared public API host. Others can run
their own and may enable layer 3 for their members.
