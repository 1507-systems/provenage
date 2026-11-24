# provenage-feeds

A tiny Cloudflare Worker that serves subscribable iCalendar (`.ics`) feeds, plus optional JSON sidecars and a feed index, from an R2 bucket. Feeds are public and read-only (GET/HEAD).

**By design it has no RSVP, attendance, auth, or member features.** Those belong to an install's member layer, not to generic calendar publishing.

## URL scheme (relative to the router mount `/provenage/`)

- `/calendar/<jurisdiction>/<body>.ics` : the feed (`text/calendar; charset=utf-8`)
- `/calendar/<jurisdiction>/all.ics` : the combined feed of a jurisdiction (same path shape)
- `/calendar/<jurisdiction>/<body>.json` : optional sidecar metadata (the set publisher does not write these)
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
5. Publish with `scripts/build_and_publish.py` (next section): `--dry-run` first, then for real.

## Publishing a feed set

`scripts/build_and_publish.py` builds a whole feed set (`python3 -m meeting_ics build-set`) and uploads it to the bucket with wrangler. It is the supported way to publish. It reads no credentials and never prints the environment; for `--target remote` wrangler uses whatever Cloudflare credentials the operator's shell already provides. `--target local` uses wrangler's local R2 simulator and needs none.

```
python3 scripts/build_and_publish.py --input records.json --config set.json --bucket <bucket> \
    --target {local,remote} [--persist-to DIR] [--dry-run] [--force] [--init-ledger] \
    [--public-base https://host/slug] [--adapter NAME --uid-namespace NS [--retrieved-at TS|now]]
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
5. Shrink guard: refuses (exit 4) a set with 0 events, a drop of more than 50% of all events, a drop of more than 50% in one jurisdiction, a disappeared jurisdiction, or a previous index it cannot read. `--force` prints each reason as `WARNING (forced)` and continues.

   **Recovery after an unreadable previous index:** if the stored `calendar/index.json` is corrupt, truncated or a different schema (for example after a failed readback left a bad object in the bucket), every run exits 4 with `previous calendar/index.json is unreadable`. That is the one case where `--force` is the recovery step: run `--dry-run` first, confirm the plan looks right (the new set has the events you expect), then run once with `--force`. The new index overwrites the bad one. Do not use `--force` for a real shrink (the other refusal reasons) until you have confirmed the drop is intended, and never as a default; it disables the guard that protects subscribers from a bad input file.
6. Prints the plan (`plan: PUT|SKIP|ORPHAN ...`; the format is stable for log parsing). `--dry-run` stops here, after every check, and writes nothing.
7. Re-reads the ledger and stops (exit 5) if it changed since step 2.
8. Uploads in this order: `state/ledger.prev.json`, `state/ledger.json` (read back and compared), the changed feeds sorted by key, then `calendar/index.json` (read back and compared). Only changed objects are uploaded, so a rerun with identical input does nothing (`no changes`).

The ledger goes first so a published SEQUENCE is never ahead of the stored ledger. If a run dies partway (exit 6, `publish incomplete`), run it again: it rebuilds byte-identical feeds from the stored ledger and uploads the rest. The index goes last, so it never lists an object that is not there.

A feed whose body leaves the input stays in the bucket and stays servable. The run reports it as `ORPHAN` and leaves it. There is no pruning in M1.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | success, including "no changes" |
| 1 | build failed, or a built file does not match its index sha256 |
| 2 | usage or config error, including a work directory or lock file that cannot be used safely |
| 3 | lock held |
| 4 | shrink guard refused |
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
