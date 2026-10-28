# Provenage

Provenage is open-source, provenance-first civic data infrastructure. It
programmatically acquires public-government records (meeting listings, agendas,
packets, minutes and video links), preserves the exact evidence and a retrieval
attestation for each one, normalizes the records into a typed graph, and exposes
them as a public API, bulk exports, RSS/Atom feeds and subscribable iCalendar feeds.

Change detection is a first-class product: for any record you can ask what changed
since the last retrieval, and see the evidence for both versions. The same install
can serve many jurisdictions, and every value it publishes carries the source, time
and hash that justify it.

## What it guarantees, and what it does not claim

Design commitments (the calendar builder implements its part today; the rest of
an install is not built yet, and these are the rules it is being built to):

- Every asserted value carries a provenance envelope: source id, source class,
  source record id, source URL, retrieved_at, content hash and derivation.
- Exact bytes are retained when the source's rights allow it. Otherwise metadata,
  hash and the authoritative link are kept.
- Disagreement between sources is preserved, not silently resolved.
- Cancellations and changes are recorded as updates, not deletions.

Not claimed:

- Provenage is not an official record. The originating government remains the
  authoritative system of record, and Provenage never claims official status.
- It does not guarantee a government's data is correct, only that it faithfully
  reports what the source published and when.

## Three layers

| Layer | What it is | Accounts | Where |
| --- | --- | --- | --- |
| 1. Generic calendar builder | Standalone, source-agnostic library that builds iCalendar. No RSVP or attendance, ever. | None | `packages/meeting-ics` |
| 2. Provenage install | A deployed instance: adapters, evidence store, normalization, graph, API, exports, canonical per-body feeds. | None for public reads | `workers/`, `adapters/` |
| 3. Member layer | RSVP, attendance, watch lists, topic flags. Only inside an install, behind accounts, delivered to members as a shared calendar, never in the public feed. | Required | Install only |

## Public calendar feeds

Feed URL scheme:

    https://api.1507.systems/provenage/calendar/<jurisdiction>/<body>.ics

Subscribe with `webcal://` in place of `https://`. Example (not yet live):

    webcal://api.1507.systems/provenage/calendar/longmont-co/city-council.ics

The host is a shared public API host operated by the maintainer. Cloudflare
Workers and R2 are the initial deployment target, not an architectural dependency.
See [docs/CALENDAR.md](docs/CALENDAR.md).

## Repository layout

- `packages/meeting-ics/` - the standalone calendar builder (Python, stdlib only).
- `workers/feeds/` - the Cloudflare Worker that serves feeds.
- `adapters/` - the adapter contract and access/legal gate (no source adapters yet).
- `docs/` - architecture, calendar rules, data model, naming, prior art.

## Status (2026-10-28)

First public commit: calendar builder and feed Worker. No feed is published yet.
The Longmont adapter is pending a permission reply, and its acquisition scripts
are not in this repository.

## Running the tests

    python3 -m unittest discover -s packages/meeting-ics -t packages/meeting-ics
    cd workers/feeds && npm ci && npm test

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Adapters are the easiest way in.
Security reports go through [SECURITY.md](SECURITY.md).

## License

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

## Name

Pronounced PROV-en-ij. See [docs/NAMING.md](docs/NAMING.md).
