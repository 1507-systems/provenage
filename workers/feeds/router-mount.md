# Router mount for the feed worker

The feed worker has no routes of its own. It is reached through the router of the
shared API host, which forwards requests under the mount slug `provenage` to it
over a service binding.

What the router needs:

- Mount prefix `/provenage`, with the prefix stripped before forwarding, so the
  worker sees mount-relative paths (`/provenage/calendar/...` becomes
  `/calendar/...`).
- Public access (no auth).
- A service binding to the worker named `provenage-feeds` (must match this
  repo's `wrangler.toml`), for example:

```toml
[[services]]
binding = "FEEDS_WORKER"
service = "provenage-feeds"
```

Deploy the feed worker before the router so the binding target exists.

## URL mapping

| Worker sees | Public URL |
|---|---|
| `/calendar/longmont-co/city-council.ics` | `https://api.1507.systems/provenage/calendar/longmont-co/city-council.ics` |
| same | `webcal://api.1507.systems/provenage/calendar/longmont-co/city-council.ics` |
| `/calendar/index.json` | `https://api.1507.systems/provenage/calendar/index.json` |

None of these URLs is live yet.
