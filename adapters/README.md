# Adapters

An adapter turns one government source into normalized records with provenance.

## Contract

Each adapter ships:

1. Source definitions: source id, source class, URLs, jurisdiction and body ids.
2. Deterministic fetch and parse logic (no hidden state, no clock-dependent output).
3. Frozen fixtures: real responses saved verbatim.
4. Expected normalized output for each fixture.
5. Jurisdiction metadata: timezone, bodies, naming conventions.
6. Licensing and reuse notes: terms, redistribution rights, attribution.

CI runs the parser over the fixtures and must reproduce the expected output exactly.

## Access and legal gate

A source must pass this before adapter code is merged:

- [ ] Access method (public API, documented feed, HTML pages) recorded.
- [ ] Terms of use read and summarized.
- [ ] Redistribution rights: may exact bytes be mirrored? If not, keep metadata,
      hash and the authoritative link only.
- [ ] Attribution requirements recorded.
- [ ] Rate limits and polite-access settings recorded.
- [ ] If robots rules disallow access: a recorded operator decision for low-volume
      reads plus a written permission request. No silent scraping.
- [ ] Aggregators: marked as optional cross-check adapters, never primary.

## Status

| Adapter | Platform | Status |
| --- | --- | --- |
| longmont-co | PrimeGov | Pending written permission; low-volume research reads only; acquisition scripts not in this repository yet |

`packages/meeting-ics` includes `primegov_normalized`, a converter from already
normalized PrimeGov meeting JSON to calendar records. It does not fetch anything.
