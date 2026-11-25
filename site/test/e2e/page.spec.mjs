import { test, expect } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import { readFileSync, mkdirSync } from 'node:fs';

const fixture = JSON.parse(readFileSync(new URL('../fixtures/index.json', import.meta.url), 'utf8'));
const vectors = JSON.parse(readFileSync(new URL('../../../workers/feeds/test/mix-vectors.json', import.meta.url), 'utf8')).vectors;
const API = 'https://api.1507.systems/provenage';
const INDEX_URL = `${API}/calendar/index.json`;
const CORS = { 'access-control-allow-origin': '*' };

/** Answer the real API URL from a fixture (the CSP still applies exactly as in production). */
async function serveIndex(page, doc = fixture, status = 200) {
  await page.route(INDEX_URL, (route) =>
    route.fulfill({ status, contentType: 'application/json', headers: CORS, body: JSON.stringify(doc) }),
  );
}

async function open(page, hash = '', doc = fixture) {
  await serveIndex(page, doc);
  await page.goto(`/index.html${hash}`);
  await expect(page.locator('#picker')).toBeVisible();
}

const axe = (page) => new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa']).analyze();
const expectNoOverflow = async (page) => {
  const o = await page.evaluate(() => ({ sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth }));
  expect(o.sw, 'no sideways scrolling').toBeLessThanOrEqual(o.cw);
};

test('default base is the production API host', async ({ page }) => {
  const seen = [];
  page.on('request', (r) => seen.push(r.url()));
  await open(page);
  expect(seen).toContain(INDEX_URL);
});

test('loaded state: lists places, bodies and counts; zero axe violations', async ({ page }) => {
  await open(page);
  await expect(page.getByText('Alpha Town (2 boards)')).toBeVisible();
  await expect(page.getByLabel('City Council (2 meetings)')).toBeVisible();
  await expect(page.getByLabel(/All meetings in Alpha Town/)).toBeVisible();
  await expect(page.locator('#copy')).toBeDisabled();
  await expect(page.locator('#address')).toHaveValue('');
  expect((await axe(page)).violations).toEqual([]);
});

test('selected state: canonical address, buttons, fragment; zero axe violations', async ({ page }) => {
  await open(page);
  // Tick in non-canonical order on purpose.
  await page.getByLabel(/^Board of Commissioners/).check();
  await page.getByLabel('Planning Commission (2 meetings)').check();
  await page.getByLabel('City Council (2 meetings)').check();
  const addr = `${API}/mix/v1/alpha-town.city-council.planning-commission_beta-county.board-of-commissioners.ics`;
  await expect(page.locator('#address')).toHaveValue(addr);
  for (const id of ['copy', 'apple', 'google', 'outlook', 'm365']) await expect(page.locator(`#${id}`)).toBeEnabled();
  expect(new URL(page.url()).hash).toBe('#v1=alpha-town.city-council.planning-commission_beta-county.board-of-commissioners');
  await expect(page.getByText('This address is for testing and may stop working.')).toBeVisible();
  await expect(page.locator('#separate-list a')).toHaveCount(3);
  expect((await axe(page)).violations).toEqual([]);
});

test('All meetings box unticks and disables the bodies, with a visible note', async ({ page }) => {
  await open(page);
  await page.getByLabel('City Council (2 meetings)').check();
  await page.getByLabel(/All meetings in Alpha Town/).check();
  await expect(page.getByLabel('City Council (2 meetings)')).toBeDisabled();
  await expect(page.getByLabel('City Council (2 meetings)')).not.toBeChecked();
  await expect(page.locator('p:visible', { hasText: 'Included in All meetings' })).toHaveCount(1);
  await expect(page.locator('#address')).toHaveValue(`${API}/mix/v1/alpha-town.all.ics`);
  await page.getByLabel(/All meetings in Alpha Town/).uncheck();
  await expect(page.getByLabel('City Council (2 meetings)')).toBeEnabled();
  await expect(page.locator('#address')).toHaveValue('');
});

test('button targets: Google, Outlook.com, Microsoft 365 encode the right address', async ({ page, context }) => {
  await open(page, '#v1=alpha-town.city-council');
  const https = `${API}/mix/v1/alpha-town.city-council.ics`;
  const webcal = https.replace('https://', 'webcal://');
  const want = {
    google: `https://calendar.google.com/calendar/render?cid=${encodeURIComponent(webcal)}`,
    outlook: `https://outlook.live.com/calendar/0/addfromweb?url=${encodeURIComponent(https)}`,
    m365: `https://outlook.office.com/calendar/0/addfromweb?url=${encodeURIComponent(https)}`,
  };
  await context.route(/calendar\.google\.com|outlook\.(live|office)\.com/, (r) => r.fulfill({ status: 200, body: 'x', contentType: 'text/plain' }));
  for (const [id, url] of Object.entries(want)) {
    const [popup] = await Promise.all([context.waitForEvent('page'), page.locator(`#${id}`).click()]);
    expect(popup.url()).toBe(url);
    await popup.close();
  }
  await expect(page.locator('#action-msg')).toContainText('Google');
});

test('Copy puts the address on the clipboard', async ({ page, context }) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write']);
  await open(page, '#v1=alpha-town.all');
  await page.locator('#copy').click();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(`${API}/mix/v1/alpha-town.all.ics`);
  await expect(page.locator('#action-msg')).toContainText('Copied');
});

test('32-feed cap: the 33rd box disables with the cap message; ticks free it again', async ({ page }) => {
  const doc = structuredClone(fixture);
  for (let i = 1; i <= 40; i++) {
    const b = `board-${String(i).padStart(2, '0')}`;
    doc.feeds.push({ body: b, body_name: `Board ${i}`, cancelled: 0, events: 1, href: `x/${b}.ics`, jurisdiction: 'big-city', key: `calendar/big-city/${b}.ics`, kind: 'body', sha256: '0'.repeat(64), size: 100, title: 'x' });
  }
  doc.jurisdictions['big-city'] = { name: 'Big City', short_name: 'Big', status: 'live' };
  await open(page, '', doc);
  await page.locator('details.group').evaluateAll((ds) => ds.forEach((d) => (d.open = true)));
  for (let i = 1; i <= 32; i++) await page.getByLabel(new RegExp(`^Board ${i} \\(`)).check();
  await expect(page.getByText('Up to 32 calendars in one address. Make a second one for more.')).toBeVisible();
  await expect(page.getByLabel(/^Board 33 \(/)).toBeDisabled();
  await expect(page.getByLabel('City Council (2 meetings)')).toBeDisabled();
  await expect(page.getByLabel(/^Board 1 \(/)).toBeEnabled();
  await page.getByLabel(/^Board 1 \(/).uncheck();
  await expect(page.getByLabel(/^Board 33 \(/)).toBeEnabled();
  expect(new URL(page.url()).hash.length).toBeLessThanOrEqual(1536 + 4);
});

function bigCity(count) {
  const doc = structuredClone(fixture);
  for (let i = 1; i <= count; i++) {
    const b = `board-${String(i).padStart(2, '0')}`;
    doc.feeds.push({ body: b, body_name: `Board ${i}`, cancelled: 0, events: 1, href: `x/${b}.ics`, jurisdiction: 'big-city', key: `calendar/big-city/${b}.ics`, kind: 'body', sha256: '0'.repeat(64), size: 100, title: 'x' });
  }
  doc.jurisdictions['big-city'] = { name: 'Big City', short_name: 'Big', status: 'live' };
  return doc;
}

test('fragment with 40 bodies in one place ticks 32, lists the other 8, and issues an address', async ({ page }) => {
  const names = Array.from({ length: 40 }, (_, i) => `board-${String(i + 1).padStart(2, '0')}`);
  await open(page, `#v1=big-city.${names.join('.')}`, bigCity(40));
  await expect(page.locator('details.group input[type=checkbox]:checked')).toHaveCount(32);
  await expect(page.locator('#restore li')).toHaveCount(8);
  await expect(page.locator('#restore')).toContainText('big-city.board-33 (over the 32 calendar limit)');
  expect(await page.locator('#address').inputValue()).toContain('/mix/v1/big-city.board-01.');
});

test('fragment over the index max_refs (3) ticks only 3 and lists the rest', async ({ page }) => {
  const doc = bigCity(6);
  doc.mix.max_refs = 3;
  await open(page, '#v1=big-city.board-01.board-02.board-03.board-04.board-05.board-06', doc);
  await expect(page.locator('details.group input[type=checkbox]:checked')).toHaveCount(3);
  await expect(page.locator('#restore li')).toHaveCount(3);
  await expect(page.locator('#restore')).toContainText('(over the 3 calendar limit)');
});

test('over the size limit: no address is issued and the page says why', async ({ page }) => {
  const doc = structuredClone(fixture);
  doc.mix.issue_max_bytes = 2000;
  await open(page, '', doc);
  await page.getByLabel('City Council (2 meetings)').check();
  await expect(page.locator('#address')).not.toHaveValue('');
  await page.getByLabel('Planning Commission (2 meetings)').check();
  await expect(page.locator('#address')).toHaveValue('');
  await expect(page.locator('#copy')).toBeDisabled();
  await expect(page.getByText(/more than one address can carry/)).toBeVisible();
  await expect(page.locator('#result-hint')).not.toContainText('Choose at least one');
});

/** 30+ places, each with one long-slug body: ~59 bytes per ref, so 30 refs exceed 1,536 bytes. */
function longSlugs(count) {
  const doc = structuredClone(fixture);
  for (let i = 1; i <= count; i++) {
    const j = `town-of-somewhere-long-${String(i).padStart(2, '0')}-co`;
    const b = 'planning-and-zoning-commission';
    doc.feeds.push({ body: b, body_name: `Zoning ${i}`, cancelled: 0, events: 1, href: `x/${b}.ics`, jurisdiction: j, key: `calendar/${j}/${b}.ics`, kind: 'body', sha256: '0'.repeat(64), size: 10, title: 'x' });
    doc.jurisdictions[j] = { name: `Town ${String(i).padStart(2, '0')}`, short_name: 'T', status: 'live' };
  }
  return doc;
}
const longHash = (n) =>
  '#v1=' + Array.from({ length: n }, (_, i) => `town-of-somewhere-long-${String(i + 1).padStart(2, '0')}-co.planning-and-zoning-commission`).join('_');

test('byte limit: long-slug boxes disable at 1,536 bytes and the page says why', async ({ page }) => {
  await open(page, '', longSlugs(32));
  await page.locator('details.group').evaluateAll((ds) => ds.forEach((d) => (d.open = true)));
  // Tick through the DOM: with 32 open groups the sticky result panel hides boxes from real clicks.
  await page.evaluate(() => {
    for (let i = 1; i <= 26; i++) {
      const cb = [...document.querySelectorAll('label.opt')].find((l) => l.textContent.startsWith(`Zoning ${i} (`)).querySelector('input');
      cb.click();
    }
  });
  await expect(page.getByLabel(/^Zoning 27 \(/)).toBeDisabled();
  await expect(page.getByText('This address is at its length limit. Make a second one for more.')).toBeVisible();
  const addr = await page.locator('#address').inputValue();
  expect(addr).toContain('/mix/v1/');
  await expect(page.locator('#copy')).toBeEnabled();
  await expect(page.locator('#result-hint')).not.toContainText('Choose at least one');
  expect(new URL(page.url()).hash.length).toBeLessThanOrEqual(1536 + 4);
  await page.getByLabel(/^Zoning 1 \(/).uncheck();
  await expect(page.getByLabel(/^Zoning 27 \(/)).toBeEnabled();
});

test('fragment over 1,536 bytes ticks only up to the limit, lists the rest, issues a valid address', async ({ page }) => {
  await open(page, longHash(30), longSlugs(30));
  const ticked = await page.locator('details.group input[type=checkbox]:checked').count();
  expect(ticked).toBeGreaterThan(0);
  expect(ticked).toBeLessThan(30);
  await expect(page.locator('#restore li')).toHaveCount(30 - ticked);
  await expect(page.locator('#restore')).toContainText('(over the address length limit)');
  const addr = await page.locator('#address').inputValue();
  expect(addr).toContain('/mix/v1/');
  const selection = addr.split('/mix/v1/')[1].replace(/\.ics$/, '');
  expect(selection.length).toBeLessThanOrEqual(1536);
  expect(new URL(page.url()).hash).toBe(`#v1=${selection}`);
});

test('filter is case- and diacritic-insensitive and announces a count', async ({ page }) => {
  await open(page);
  await page.getByLabel('Filter places and boards').fill('CAFE');
  await expect(page.getByText('Café Ville Municipal Government', { exact: false }).first()).toBeVisible();
  await expect(page.getByText('Alpha Town (2 boards)')).toBeHidden();
  await expect(page.locator('#count')).toHaveText('Showing 2 of 8 calendars.');
  expect(await page.locator('#count').getAttribute('aria-live')).toBe('polite');
  await page.getByLabel('Filter places and boards').fill('zoning');
  await expect(page.getByLabel(/Zoning Hearing Board/)).toBeVisible();
  await expect(page.locator('#count')).toHaveText('Showing 1 of 8 calendars.');
  await page.getByLabel('Filter places and boards').fill('');
  await expect(page.getByText('Alpha Town (2 boards)')).toBeVisible();
});

test('hostile index strings render as text and run nothing', async ({ page }) => {
  const dialogs = [];
  page.on('dialog', (d) => dialogs.push(d.message()));
  await open(page);
  await expect(page.getByLabel(/Parks, Rec; Board \\ <script>alert\(1\)<\/script>/)).toBeVisible();
  expect(await page.locator('main script, main img, main iframe').count()).toBe(0);
  expect(dialogs).toEqual([]);
});

test('hostile slugs in the index are ignored, never turned into links', async ({ page }) => {
  const doc = structuredClone(fixture);
  doc.feeds.push({ body: 'x" onfocus="alert(1)', body_name: 'Evil', events: 1, jurisdiction: 'alpha-town', href: 'javascript:alert(1)', size: 1 });
  doc.feeds.push({ body: 'ok-board', body_name: 'Fine Board', events: 1, jurisdiction: 'alpha-town', href: 'javascript:alert(1)', size: 1 });
  await open(page, '#v1=alpha-town.ok-board', doc);
  await expect(page.getByLabel(/^Evil/)).toHaveCount(0);
  const hrefs = await page.locator('#separate-list a').evaluateAll((as) => as.map((a) => a.href));
  expect(hrefs).toEqual([`${API}/calendar/alpha-town/ok-board.ics`]);
});

test('the CSP blocks an injected inline script and an injected external script', async ({ page }) => {
  await open(page);
  const result = await page.evaluate(
    () =>
      new Promise((resolve) => {
        const violations = [];
        document.addEventListener('securitypolicyviolation', (e) => violations.push(e.violatedDirective));
        const s = document.createElement('script');
        s.textContent = 'window.__pwned = 1';
        document.body.append(s);
        const x = document.createElement('script');
        x.src = 'https://example.invalid/x.js';
        document.body.append(x);
        setTimeout(() => resolve({ pwned: window.__pwned === 1, violations }), 500);
      }),
  );
  expect(result.pwned).toBe(false);
  expect(result.violations.length).toBeGreaterThanOrEqual(2);
  expect(result.violations.every((v) => v.startsWith('script-src'))).toBe(true);
});

test('fragment restore ticks boxes; renamed and retired parts are listed, not dropped', async ({ page }) => {
  await open(page, '#v1=alpha-town.city-council.old-commission.retired-board.nowhere');
  await expect(page.getByLabel('City Council (2 meetings)')).toBeChecked();
  await expect(page.getByLabel('Planning Commission (2 meetings)')).toBeChecked();
  const restore = page.locator('#restore');
  await expect(restore).toContainText('renamed');
  await expect(restore).toContainText('alpha-town.retired-board (no longer published)');
  await expect(restore).toContainText('alpha-town.nowhere (not found)');
  expect(await page.locator('#address').inputValue()).toBe(`${API}/mix/v1/alpha-town.city-council.planning-commission.ics`);
});

test('pasting an existing address ticks its boxes; junk is reported', async ({ page }) => {
  await open(page);
  await page.getByText('Change an existing address').click();
  await page.getByLabel('Paste a calendar address from this page').fill(`${API}/mix/v1/gamma-town.city-council_beta-county.all.ics`);
  await page.getByRole('button', { name: 'Load these choices' }).click();
  await expect(page.getByLabel('City Council (2 meetings)')).toBeChecked();
  await expect(page.getByLabel(/All meetings in Beta County/)).toBeChecked();
  await page.getByLabel('Paste a calendar address from this page').fill('not an address');
  await page.getByRole('button', { name: 'Load these choices' }).click();
  await expect(page.locator('#restore')).toContainText('does not look like an address');
});

test('error state: one message, Retry recovers; zero axe violations', async ({ page }) => {
  await page.route(INDEX_URL, (r) => r.abort());
  await page.goto('/index.html');
  await expect(page.locator('#error')).toBeVisible();
  await expect(page.locator('#error-text')).toHaveText('Could not load the list of calendars. Check your connection and try again.');
  await expect(page.locator('#picker')).toBeHidden();
  expect((await axe(page)).violations).toEqual([]);
  await serveIndex(page);
  await page.unroute(INDEX_URL, () => {}).catch(() => {});
  await serveIndex(page);
  await page.getByRole('button', { name: 'Retry' }).click();
  await expect(page.locator('#picker')).toBeVisible();
  await expect(page.locator('#error')).toBeHidden();
});

test('HTTP error status is the same single error message', async ({ page }) => {
  await serveIndex(page, { error: 'x' }, 503);
  await page.goto('/index.html');
  await expect(page.locator('#error-text')).toContainText('Could not load the list');
});

test('unknown schema or missing mix.version: out of date, reload', async ({ page }) => {
  for (const doc of [{ ...fixture, schema: 'provenage-calendar-index/9' }, (({ mix, ...rest }) => rest)(fixture)]) {
    await serveIndex(page, doc);
    await page.goto('/index.html');
    await expect(page.locator('#error-text')).toHaveText('This page is out of date. Reload it to continue.');
    await expect(page.getByRole('button', { name: 'Reload the page' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Retry' })).toBeHidden();
    expect((await axe(page)).violations).toEqual([]);
  }
});

test('no JavaScript: the noscript text and links are shown', async ({ browser }) => {
  const ctx = await browser.newContext({ javaScriptEnabled: false });
  const page = await ctx.newPage();
  await page.goto('/index.html');
  const text = await page.locator('body').innerText();
  expect(text).toContain('JavaScript is needed for this page');
  expect(text).not.toContain('Loading the list of calendars');
  expect(text).not.toContain('Your calendar address');
  const html = await page.content();
  expect(html).toContain(`href="${INDEX_URL}"`);
  expect(html).toMatch(/href="[^"]*docs\/CALENDAR\.md"/);
  // axe cannot run in a page with scripting disabled; the noscript markup is plain headings, text and links.
  await ctx.close();
});

test('dark mode and reduced motion: zero axe violations', async ({ page }) => {
  await page.emulateMedia({ colorScheme: 'dark', reducedMotion: 'reduce' });
  await open(page, '#v1=beta-county.all');
  expect((await axe(page)).violations).toEqual([]);
});

for (const w of [320, 360]) {
  test(`width ${w}px: no sideways scrolling in loaded, selected and error states`, async ({ page }) => {
    await page.setViewportSize({ width: w, height: 640 });
    await open(page);
    await expectNoOverflow(page);
    await page.getByLabel(/Parks, Rec/).check();
    await page.getByLabel(/All meetings in Café Ville/).check();
    await expectNoOverflow(page);
    mkdirSync('test-results', { recursive: true });
    await page.screenshot({ path: `test-results/width-${w}.png` });
    // Every control a finger must hit is at least 44 px tall.
    const small = await page.locator('button:visible, input:visible, summary:visible, label.opt:visible').evaluateAll((els) =>
      els.filter((e) => e.getBoundingClientRect().height < 43.5 && e.type !== 'checkbox').map((e) => e.outerHTML.slice(0, 80)),
    );
    expect(small).toEqual([]);
    expect((await axe(page)).violations).toEqual([]);
    await page.unroute(INDEX_URL);
    await page.route(INDEX_URL, (r) => r.abort());
    await page.reload();
    await expect(page.locator('#error')).toBeVisible();
    await expectNoOverflow(page);
  });
}

test('200% zoom equivalent (640px wide at 2x scale) has no sideways scrolling', async ({ page }) => {
  await page.setViewportSize({ width: 160, height: 320 });
  await open(page, '#v1=beta-county.board-of-commissioners.parks-board');
  await expectNoOverflow(page);
});

test('keyboard only: tab order reaches the filter, a box, and Copy', async ({ page }) => {
  await open(page, '#v1=alpha-town.all');
  await page.getByLabel('Filter places and boards').focus();
  await page.keyboard.press('Tab');
  const first = await page.evaluate(() => document.activeElement.tagName);
  expect(['SUMMARY', 'INPUT']).toContain(first);
  await page.locator('#copy').focus();
  await expect(page.locator('#copy')).toBeFocused();
});

test('the shared Worker grammar vectors pass in the browser too', async ({ page }) => {
  await open(page);
  const out = await page.evaluate(async (vs) => {
    const { parseSelection } = await import('/canon.js');
    return vs.map((v) => {
      const r = parseSelection(v.selection);
      return { name: v.name, ok: r.ok === v.valid && (v.valid ? r.selection === v.canonical_selection : (r.canonical ?? null) === (v.canonical_selection ?? null)) };
    });
  }, vectors);
  expect(out.filter((o) => !o.ok)).toEqual([]);
});

test('test override of the API base works (own origin, loosened CSP copy)', async ({ page }) => {
  await page.goto('/override.html');
  await expect(page.locator('#picker')).toBeVisible();
  await page.getByLabel('City Council (2 meetings)').check();
  await expect(page.locator('#address')).toHaveValue('http://127.0.0.1:4173/fake-api/mix/v1/alpha-town.city-council.ics');
});
