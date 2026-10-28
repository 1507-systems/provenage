# Contributing to Provenage

Thanks for helping. Provenage is provenance-first civic data infrastructure; read
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) first if you have not.

## Sign-off (DCO)

Every commit must carry a Developer Certificate of Origin sign-off:

    git commit -s

This adds a `Signed-off-by:` line and certifies you have the right to submit the
work under the project license (https://developercertificate.org). There is no CLA.

## Commits and pull requests

- Use conventional commits: `feat:`, `fix:`, `docs:`, `test:`, `refactor:`,
  `chore:`, with an optional scope, for example `feat(meeting-ics): ...`.
- Keep pull requests focused. Describe what changed and why.
- Tests are required for behavior changes. CI must pass.
- No secrets, credentials or personal data in code, fixtures or commit history.
  Fixtures are frozen copies of public records; strip anything personal that a
  public notice does not need.

## Adapters are the easiest contribution

A source adapter turns one government source into normalized records. Each adapter
ships:

1. Source definitions (source id, source class, URLs, jurisdiction and body ids).
2. Deterministic fetch and parse logic.
3. Frozen fixtures of real responses.
4. Expected normalized output for those fixtures.
5. Jurisdiction metadata (timezone, bodies, locale conventions).
6. Licensing and reuse notes for the source.

CI proves determinism: running the parser over the frozen fixtures must reproduce
the expected output byte for byte. See [adapters/README.md](adapters/README.md).

## Access and legal gate

Every new source must pass the access/legal gate before any adapter code is merged:
access method, terms of use, redistribution rights, attribution requirements and
rate limits, recorded in the adapter's notes. Sources with blanket robots
disallow are handled only under a recorded operator decision plus a written
permission request. The checklist is in [adapters/README.md](adapters/README.md).

## Ontology and schema changes

Changes to the data model, field contract or provenance envelope get stricter
review: they affect every adapter and every consumer. Open an issue first, explain
the case, and expect to update docs/DATA-MODEL.md and fixtures in the same pull
request.

## Calendar package

`packages/meeting-ics` is Python standard library only. Do not add third-party
dependencies. It stays source-agnostic, with no accounts and no RSVP or
attendance, ever.

## Running tests

    python3 -m unittest discover -s packages/meeting-ics -t packages/meeting-ics
    cd workers/feeds && npm ci && npm test

## Conduct and security

See [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md). Report vulnerabilities privately as
described in [SECURITY.md](SECURITY.md).
