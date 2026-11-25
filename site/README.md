# Builder page

The calendar builder: a static page (plain HTML, CSS and an ES module; no build step, no dependencies) that
reads `https://api.1507.systems/provenage/calendar/index.json` and issues one `/mix/v1/` address for the meetings
a person picks. See `docs/CALENDAR.md` for the address rules; `canon.js` is the browser copy of the Worker's
grammar (`workers/feeds/src/mix/parse.ts`), checked against the shared vectors in
`workers/feeds/test/mix-vectors.json`.

Safety rules the tests enforce: strict CSP (meta tag), no HTML-parsing APIs, index strings only via `textContent`,
links built only from validated slugs, no third-party code.

The API base can be overridden for tests only by a `<meta name="provenage-base">` tag, which the shipped page
does not contain (and the CSP would block any other host anyway).

## Tests (validation only)

```
cd site
npm ci
npm run test:unit                 # grammar vectors, banned-API grep, CSP text
npx playwright install chromium   # once
npm run test:e2e                  # axe, 320 px width, XSS, CSP, states (API answered from test/fixtures/index.json)
```

`test/fixtures/index.json` is synthetic (fictional places, one hostile body name).
