# provenage-feeds

A tiny Cloudflare Worker that serves subscribable iCalendar (`.ics`) feeds, plus optional JSON sidecars and a feed index, from an R2 bucket. Feeds are public and read-only (GET/HEAD).

**By design it has no RSVP, attendance, auth, or member features.** Those belong to an install's member layer, not to generic calendar publishing.

## URL scheme (relative to the router mount `/provenage/`)

| Route (Worker path) | Methods | Notes |
|---|---|---|
| `/calendar/index.json` | GET, HEAD | The feed index, from an index object in R2. The only route with `Access-Control-Allow-Origin: *` (200, 304, HEAD). |
| `/calendar/<jurisdiction>/<body>.ics` | GET, HEAD | One body's feed (`text/calendar; charset=utf-8`). No CORS. |
| `/calendar/<jurisdiction>/all.ics` | GET, HEAD | The combined feed of a jurisdiction (same path shape). |
| `/calendar/<jurisdiction>/<body>.json` | GET, HEAD | Optional sidecar metadata (the set publisher does not write these). |
| `/mix/v1/<selection>.ics` | GET, HEAD | One calendar merged from several feeds. Matched before the feed route; any path starting `/mix/` goes to it. Grammar, limits and merge rules: `docs/CALENDAR.md`. No CORS. |
| anything else | any | 404 JSON; other methods 405 with `Allow: GET, HEAD`; `OPTIONS` stays 405. |

Example: `https://api.1507.systems/provenage/calendar/longmont-co/city-council.ics` (or `webcal://`).
Jurisdiction slugs are `<place>-<2-letter state>` (`longmont-co`); see `docs/CALENDAR.md`.

Feed and index responses carry ETag, Last-Modified, `Cache-Control: public, max-age=300`, `X-Content-Type-Options: nosniff`; `If-None-Match` yields 304. `/mix/v1/` responses (200, 304, 404, 503) are described in `docs/CALENDAR.md`; its 404 and 503 bodies are JSON with `Cache-Control: no-store`, and a 503 carries `Retry-After` (300, or 60 when a source feed fails its `sha256` check). Bucket names are never exposed. The slug is not in the handler code; only the router config and the publish script know it.

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
5. Publish with `scripts/build_and_publish.py` (next section): `--dry-run` first, then for real.

## Publishing a feed set

`scripts/build_and_publish.py` builds a whole feed set (`python3 -m meeting_ics build-set`) and uploads it to the bucket with wrangler. It is the supported way to publish. It reads no credentials and never prints the environment; for `--target remote` wrangler uses whatever Cloudflare credentials the operator's shell already provides. `--target local` uses wrangler's local R2 simulator and needs none.

```
python3 scripts/build_and_publish.py --input records.json --config set.json --bucket <bucket> \
    --target {local,remote} [--persist-to DIR] [--dry-run] [--force] [--init-ledger] \
    [--more-input FILE ...] [--public-base https://host/slug] [--adapter NAME --uid-namespace NS [--retrieved-at TS|now]]
```

`--target` has no default. `--work-dir` is optional: by default the script builds in a fresh private temporary directory and removes it afterwards. A directory you name must be new, empty, or one this script already used (it carries a `.provenage-publish-workdir` marker file); anything else is refused (exit 2) and left untouched. Only `out/` and `ledger.json` inside it are ever rewritten. `--lock-file` defaults to a private per-user directory under the system temp directory; the lock file is opened without following symlinks. `--persist-to` is only for `local`. `--adapter`, `--uid-namespace` and `--retrieved-at` are passed to the builder unchanged. `--public-base` prints `Public URL: <base>/<key>` for each feed after a successful (non-dry) run. Run `npm ci` once first: the script uses the wrangler in `node_modules/.bin`.

### Keys

| Key | Content | Served by the Worker |
|---|---|---|
| `calendar/index.json` | feed index, schema `provenage-calendar-index/1` | yes |
| `calendar/<jurisdiction>/<body>.ics` | one body's feed | yes |
| `calendar/<jurisdiction>/all.ics` | every meeting of the jurisdiction | yes |
| `state/ledger.json` | the SEQUENCE ledger, one per bucket | **never** (Worker tests V1) |
| `state/ledger.prev.json` | the previous ledger, one version back | **never** |

The ledger must stay under `state/`. Never put it under `calendar/`: `calendar/<x>/<y>.json` is a served path.

### What a run does

1. Takes a local lock (exit 3 if another publish holds it).
2. Reads `calendar/index.json` and `state/ledger.json`. A failed read is never treated as an empty ledger. A missing ledger next to an existing index is refused (exit 5) unless you pass `--init-ledger`, which resets every SEQUENCE. **The first publish to a remote bucket needs `--init-ledger`** (with `--target remote` a missing ledger is always refused without it), because a "missing" read over the network is not proof that the bucket is new. Use it only for a bucket you know is new, and not again afterwards. The key is treated as missing only when wrangler's output positively says the key does not exist (`NoSuchKey`, code 10007, or the message "The specified key does not exist"); any other failure, including a missing bucket or an authentication error, is a storage error (exit 6). If a real not-found is reported as exit 6, check the wrangler output printed with the error.
3. Builds into a work directory (the builder runs offline, from the package directory with absolute paths, `python -E`, and only an allowlisted environment: no `CLOUDFLARE_*` or other token-like variable reaches it; only wrangler gets your environment). Every built feed must match its index sha256.
4. Checks the new ledger against the old one: no UID removed, no SEQUENCE lowered (exit 5).
5. Shrink guard: refuses (exit 4) a set with 0 events, a drop of more than 50% of all events, a drop of more than 50% in one jurisdiction, a disappeared jurisdiction, a previous index it cannot read, and two permanence reasons that protect issued `/mix/v1/` addresses (below). `--force` prints each reason as `WARNING (forced)` and continues.

   **Recovery after an unreadable previous index:** if the stored `calendar/index.json` is corrupt, truncated or a different schema (for example after a failed readback left a bad object in the bucket), every run exits 4 with `previous calendar/index.json is unreadable`. That is the one case where `--force` is the recovery step: run `--dry-run` first, confirm the plan looks right (the new set has the events you expect), then run once with `--force`. The new index overwrites the bad one. Do not use `--force` for a real shrink (the other refusal reasons) until you have confirmed the drop is intended, and never as a default; it disables the guard that protects subscribers from a bad input file.
6. Prints the plan (`plan: PUT|SKIP|ORPHAN ...`; the format is stable for log parsing). `--dry-run` stops here, after every check, and writes nothing.
7. Re-reads the ledger and stops (exit 5) if it changed since step 2.
8. Uploads in this order: `state/ledger.prev.json`, `state/ledger.json` (read back and compared), the changed feeds sorted by key, then `calendar/index.json` (read back and compared). Only changed objects are uploaded, so a rerun with identical input does nothing (`no changes`).

The ledger goes first so a published SEQUENCE is never ahead of the stored ledger. If a run dies partway (exit 6, `publish incomplete`), run it again: it rebuilds byte-identical feeds from the stored ledger and uploads the rest. The index goes last, so it never lists an object that is not there.

**Permanence guards (exit 4).** Compared with the previous index, and only when that index is readable:

- `alias <body|jurisdiction> <slug> was removed (aliases are append-only)` and `... was changed from <old> to <new> (aliases are append-only)`: every alias in the previous index must be present and unchanged in the new one. Adding aliases is always allowed.
- `feed calendar/<j>/<b>.ics disappeared without an alias`: every feed key in the previous index must still be in the new one, or be covered by an alias the way the Worker resolves it (a body alias, or a jurisdiction alias that is `null` or whose target feed exists in the new index). A feed in a jurisdiction the **previous** index marks `status: "dev"` is exempt from this check, so a dev jurisdiction's bodies can be renamed, replaced or removed. The new index cannot grant itself that exemption. Dropping a whole dev jurisdiction still trips the shrink guard (`jurisdiction <j> disappeared`, and the 50% total-drop check if it applies). Confirm with `--dry-run`, then publish once with `--force`.

**Resolving exit 4 for a renamed or removed body: add an alias, not `--force`.** Suppose `longmont-co/planning-commission` is renamed `planning-and-zoning-commission` in the source data. Put the old slug in the set config, then dry-run and publish:

```
"aliases": {"bodies": {"longmont-co/planning-commission": "longmont-co/planning-and-zoning-commission"}}
```

Use `null` as the target to retire a body (`"longmont-co/old-task-force": null`) and `"jurisdictions": {"old-slug": "new-slug"}` for a whole jurisdiction. The builder (`build-set`, exit 2) checks that an alias source is not a current feed or jurisdiction, that a non-null target is in the set, and that a target is not itself an alias source (no chains). Aliases are permanent: keep them in the config forever. `--force` is not the fix: it would let the old slug vanish unredirected, and an address issued with it would silently lose that calendar. Use it only for a change you have confirmed is intended and no address could depend on.

**Orphans.** A feed that is in the previous index but not the new one stays in the bucket and stays servable at its direct `/calendar/<j>/<b>.ics` address, frozen at its last content. The run reports it as `plan: ORPHAN <key> (in previous index, not in new; left in place)` once, on the run that drops it from the index. Because the new index no longer lists it, `/mix/v1/` does not use it: an issued mix address reaches the renamed feed through its alias, and counts a retired (`null`) or unaliased vanished feed as missing (`X-Provenage-Missing`). Aliases do not rewrite direct feed addresses. There is no pruning.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | success, including "no changes" |
| 1 | build failed, or a built file does not match its index sha256 |
| 2 | usage or config error, including a work directory or lock file that cannot be used safely |
| 3 | lock held |
| 4 | publisher guard refused (shrink, alias removed or changed, feed disappeared without an alias) |
| 5 | ledger integrity refused |
| 6 | storage or file-system error (wrangler failure, readback mismatch, disk or permission problem while running) |
| 7 | unexpected internal error (a bug or unexpected builder output; never reported as exit 1) |

### Limits to know

- One publishing host. wrangler has no conditional write, so two publishers racing between the pre-write check and the ledger upload are not detected.
- DTSTAMP is the record's `retrieved_at`, so a new fetch changes every feed's bytes and every feed is re-uploaded.
- `--dry-run` still reads the bucket (it needs the previous index and ledger), so with `--target remote` it needs credentials, but it never writes.

### `publish-feed.sh`

`scripts/publish-feed.sh` is unchanged and kept as a manual tool for uploading one `.ics` file to a key you name. It does not touch the index or the ledger, has no shrink guard or readback, and hardcodes the public host in its printed URLs. Do not use it on a bucket the set publisher manages for routine publishing: the set publisher compares each feed to the index sha256, so the next set publish simply overwrites a hand-uploaded feed with the built one.

## Tests

- `python -m pytest workers/feeds/scripts` : in-memory store tests (P*). No network.
- `PROVENAGE_WRANGLER_LOCAL=1 python -m pytest workers/feeds/scripts -k WranglerLocal` : the same flow against wrangler's local R2 simulator (needs `npm ci`). Local only; nothing remote is ever touched.

## Index cache, CORS and logging

- `GET/HEAD /calendar/index.json` is the only route with `Access-Control-Allow-Origin: *` (200, 304 and HEAD). Feeds and error responses carry no CORS header; `OPTIONS` stays 405.
- The index is held in memory per isolate, rechecked against R2 at most every 60 s (conditional read), and the last good copy is served for up to 1 h if R2 fails (503 after that).
- Observability is off in `wrangler.toml` and `src/` must not call `console.*`; a test enforces both.
- `/mix/v1/` keeps a per-isolate cache of parsed source feeds keyed by the index `sha256` (content-addressed, at most 32 MiB), and no composite body cache and no Cache API. Every source read is verified against its index `sha256`. At most 1 index read plus 32 source reads per uncached request.
