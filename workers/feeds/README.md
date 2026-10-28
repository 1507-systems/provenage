# provenage-feeds

A tiny Cloudflare Worker that serves subscribable iCalendar (`.ics`) feeds, plus optional JSON sidecars and a feed index, from an R2 bucket. Feeds are public and read-only (GET/HEAD).

**By design it has no RSVP, attendance, auth, or member features.** Those belong to an install's member layer, not to generic calendar publishing.

## URL scheme (relative to the router mount `/provenage/`)

- `/calendar/<jurisdiction>/<body>.ics` : the feed (`text/calendar; charset=utf-8`)
- `/calendar/<jurisdiction>/<body>.json` : optional sidecar metadata
- `/calendar/index.json` : list of available feeds (from an index object in R2)
- anything else : 404 JSON

Example: `https://api.1507.systems/provenage/calendar/longmont-co/city-council.ics` (or `webcal://`).

Responses carry ETag, Last-Modified, `Cache-Control: public, max-age=300`, `X-Content-Type-Options: nosniff`; `If-None-Match` yields 304. Bucket names are never exposed. The slug is not in the handler code; only the router config and the publish script know it.

## Refresh semantics

Calendar clients poll on their own schedule; the worker just serves the latest object. Updates to existing events are picked up by clients when the builder bumps `SEQUENCE` (and `LAST-MODIFIED`) in the .ics; the worker does not alter feed content.

## Develop

```
npm ci
npm test
npm run typecheck
```

Tests use a fake R2 binding and run in plain Node via vitest. They can be moved to `@cloudflare/vitest-pool-workers` unchanged.

## Deploy (later; operator runs these)

1. Create the R2 bucket and set `bucket_name` in `wrangler.toml` if it differs from the placeholder `provenage-feeds`.
2. Set `account_id` in `wrangler.toml` (or the `CLOUDFLARE_ACCOUNT_ID` environment variable) for your account.
3. `npx wrangler deploy`
4. Add the mount and service binding to the router of the shared API host per `router-mount.md`, then deploy the router.
5. `FEEDS_BUCKET=<bucket> FEEDS_KEY=calendar/<jurisdiction>/<body>.ics bash scripts/publish-feed.sh --dry-run FILE.ics`, then without `--dry-run`. Also upload `calendar/index.json`.
