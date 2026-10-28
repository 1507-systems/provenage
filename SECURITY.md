# Security Policy

## Reporting a vulnerability

Report privately through GitHub security advisories on this repository
(Security tab, "Report a vulnerability"). Please do not open a public issue or
pull request for a suspected vulnerability.

Include what you found, how to reproduce it, and which feed, endpoint or
component is affected.

## What counts

- Feeds or API responses serving wrong or tampered data, including calendar
  feeds that show a meeting time, status or link that does not match the
  retained evidence.
- Provenance forgery: any way to make a value appear to come from a source,
  retrieval time or content hash it did not come from.
- Issues in the Cloudflare Worker code (authentication bypass, cache poisoning,
  request smuggling, unintended exposure of stored objects).
- Leakage of member-layer data (RSVPs, watch lists, topic flags) into public
  outputs.

Disagreement with a government source, or a government publishing a wrong
record, is not a vulnerability in Provenage. Open a normal issue instead.

## Response

Target: an initial response within 7 days. Fixes and disclosure timing are
agreed with the reporter where practical.

## Bounty

There is no bounty program. Credit in the advisory is offered if you want it.

## Supported versions

Pre-release. Only the main branch is supported.
