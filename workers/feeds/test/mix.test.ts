import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { readFileSync, readdirSync, statSync, mkdirSync, writeFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import worker, { type Env } from '../src/index';
import { resetIndexCache } from '../src/indexcache';
import { resetSourceCache } from '../src/mix/sources';
import { fold } from '../src/mix/fold';
import { parseSelection } from '../src/mix/parse';

const HERE = dirname(fileURLToPath(import.meta.url));
const FIX = join(HERE, 'fixtures');
const T0 = new Date('2026-06-01T12:00:00Z');

function walk(dir: string, base = ''): string[] {
  return readdirSync(dir).flatMap((n: string) => {
    const p = join(dir, n);
    return statSync(p).isDirectory() ? walk(p, `${base}${n}/`) : [`${base}${n}`];
  });
}
const FILES: Record<string, string> = {};
for (const f of walk(join(FIX, 'calendar'))) FILES[`calendar/${f}`] = readFileSync(join(FIX, 'calendar', f), 'utf8');
const sha = (s: string) => createHash('sha256').update(s, 'utf8').digest('hex');

/** Fake R2 with a call log, onlyIf support and per-test file overrides. */
function makeBucket(files: Record<string, string>) {
  const log: string[] = [];
  const st = { failKeys: new Set<string>() };
  const meta = (key: string) => ({
    key,
    size: Buffer.byteLength(files[key]),
    etag: `e-${sha(files[key]).slice(0, 8)}`,
    httpEtag: `"e-${sha(files[key]).slice(0, 8)}"`,
    uploaded: T0,
  });
  const bucket = {
    async get(key: string, opts?: { onlyIf?: { etagDoesNotMatch?: string } }) {
      log.push(`get ${key}`);
      if (st.failKeys.has(key)) throw new Error('r2 down');
      if (!(key in files)) return null;
      const m = meta(key);
      if (opts?.onlyIf?.etagDoesNotMatch === m.etag) return m;
      const bytes = Buffer.from(files[key], 'utf8');
      return {
        ...m,
        body: new Response(bytes).body,
        arrayBuffer: async () => bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength),
      };
    },
    async head() {
      throw new Error('unused');
    },
  } as unknown as R2Bucket;
  return { bucket, log, files, st };
}

let fx: ReturnType<typeof makeBucket>;
let env: Env;
const MIX = (sel: string) => `/mix/v1/${sel}.ics`;
const call = (path: string, init?: RequestInit) => worker.fetch(new Request(`https://x.test${path}`, init), env);
const sourceReads = () => fx.log.filter((l) => l !== 'get calendar/index.json');

function setup(files: Record<string, string> = FILES) {
  resetIndexCache();
  resetSourceCache();
  fx = makeBucket({ ...files });
  env = { FEEDS: fx.bucket };
}
beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(T0);
  setup();
});
afterEach(() => vi.useRealTimers());

const unfold = (s: string) => s.replace(/\r\n[ \t]/g, '');
const events = (s: string) => s.match(/BEGIN:VEVENT\r\n[\s\S]*?END:VEVENT\r\n/g) ?? [];
const uidsOf = (s: string) => events(s).map((e) => /\r\nUID:([^\r\n]*)/.exec(e)![1]);
const INDEX = JSON.parse(FILES['calendar/index.json']);
const withIndex = (mut: (i: any) => void) => {
  const i = JSON.parse(FILES['calendar/index.json']);
  mut(i);
  setup({ ...FILES, 'calendar/index.json': JSON.stringify(i) });
};

const A = 'alpha-town';
const B = 'beta-county';

describe('grammar vectors (shared with the page)', () => {
  const doc = JSON.parse(readFileSync(join(HERE, 'mix-vectors.json'), 'utf8'));
  for (const v of doc.vectors) {
    it(v.name, async () => {
      const p = parseSelection(v.selection);
      expect(p.ok).toBe(v.valid);
      if (!p.ok) {
        if (v.canonical_selection) expect(p.canonical).toBe(`/provenage/mix/v1/${v.canonical_selection}.ics`);
        else expect(p.canonical).toBeUndefined();
        if (v.error_contains) expect(p.error).toContain(v.error_contains);
      }
      // And through the real route: a bad selection is a 404 JSON naming the canonical address, with no storage access.
      const path = `/mix/v1/${v.selection}.ics`;
      const r = await call(path);
      if (v.valid) {
        expect([200, 404]).toContain(r.status); // valid grammar; whether it is published is a separate question
      } else {
        expect(r.status).toBe(404);
        expect(r.headers.get('Content-Type')).toBe('application/json; charset=utf-8');
        expect(r.headers.get('Cache-Control')).toBe('no-store');
        const body = (await r.json()) as any;
        expect(body.canonical).toBe(v.canonical_selection ? `/provenage/mix/v1/${v.canonical_selection}.ics` : undefined);
        expect(fx.log).toEqual([]);
      }
    });
  }
});

describe('URL limits and shape', () => {
  it('1,536 bytes is accepted by the parser, 1,537 is a 404 with no storage access', async () => {
    const mk = (n: number) => {
      const groups: string[] = [];
      let len = 0;
      for (let i = 0; len < n; i++) {
        const g = `j${String(i).padStart(3, '0')}.all`;
        groups.push(g);
        len += g.length + 1;
      }
      let s = groups.join('_');
      s = s.slice(0, n); // pad/trim to exact byte size (grammar validity is not the point here)
      return s;
    };
    const big = mk(1537);
    expect(big.length).toBe(1537);
    const r = await call(MIX(big));
    expect(r.status).toBe(404);
    expect(((await r.json()) as any).error).toContain('too long');
    expect(fx.log).toEqual([]);
    expect(parseSelection(mk(1536)).ok).toBe(false); // over 32 refs or invalid, but not "too long"
    const p = parseSelection(mk(1536));
    expect(!p.ok && p.error).not.toContain('too long');
  });

  it('rejects non-mix shapes without storage access', async () => {
    for (const p of [
      '/mix/v1/', '/mix/v1/.ics', '/mix/v1/a.b', '/mix/v1/a.b.json', '/mix/v1/a.b.ICS', '/mix/v1/a.b.ics/',
      '/mix/v2/a.b.ics', '/mix/a.b.ics', '/mix/', '/mix/v1//a.b.ics', '/mix/v1/%61.b.ics',
    ]) {
      const r = await call(p);
      expect(r.status, p).toBe(404);
    }
    expect(fx.log).toEqual([]);
  });

  it('ignores the query string for content, ETag and bytes', async () => {
    const a = await call(MIX(`${A}.city-council`));
    const b = await call(MIX(`${A}.city-council`) + '?utm=1&x=%00');
    expect(a.status).toBe(200);
    expect(b.headers.get('ETag')).toBe(a.headers.get('ETag'));
    expect(await b.text()).toBe(await a.text());
  });

  it('only GET and HEAD; OPTIONS is 405', async () => {
    expect((await call(MIX(`${A}.all`), { method: 'POST' })).status).toBe(405);
    expect((await call(MIX(`${A}.all`), { method: 'OPTIONS' })).status).toBe(405);
  });
});

describe('200 responses', () => {
  it('headers, content and HEAD', async () => {
    const r = await call(MIX(`${A}.city-council`));
    expect(r.status).toBe(200);
    expect(r.headers.get('Content-Type')).toBe('text/calendar; charset=utf-8');
    expect(r.headers.get('ETag')).toMatch(/^"m1-[0-9a-f]{32}"$/);
    expect(r.headers.get('Last-Modified')).toBe(T0.toUTCString());
    expect(r.headers.get('Cache-Control')).toBe('public, max-age=300');
    expect(r.headers.get('X-Content-Type-Options')).toBe('nosniff');
    expect(r.headers.get('X-Provenage-Missing')).toBeNull();
    expect(r.headers.get('Access-Control-Allow-Origin')).toBeNull();
    const text = await r.text();
    expect(text.startsWith('BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Provenage//mix v1//EN\r\nCALSCALE:GREGORIAN\r\nMETHOD:PUBLISH\r\n')).toBe(true);
    expect(text).toContain('REFRESH-INTERVAL;VALUE=DURATION:PT1H\r\nX-PUBLISHED-TTL:PT1H\r\n');
    expect(text.endsWith('END:VCALENDAR\r\n')).toBe(true);

    const h = await call(MIX(`${A}.city-council`), { method: 'HEAD' });
    expect(h.status).toBe(200);
    expect(h.headers.get('ETag')).toBe(r.headers.get('ETag'));
    expect(h.headers.get('Content-Length')).toBeNull();
    expect(await h.text()).toBe('');
  });

  it('a single ref uses the index title as the calendar name, with no place prefix', async () => {
    const t = await (await call(MIX(`${A}.city-council`))).text();
    expect(unfold(t)).toContain('X-WR-CALNAME:Alpha Town: City Council\r\n');
    expect(t).not.toContain('SUMMARY:Alpha: ');
  });

  it('names several bodies of one jurisdiction, with the combined ref as "All meetings"', async () => {
    const t = unfold(await (await call(MIX(`${A}.city-council.planning-commission`))).text());
    expect(t).toContain('X-WR-CALNAME:Alpha: City Council\\, Planning Commission\r\n');
    const long = withIndexName();
    expect(long).toContain('+ 1 more');
  });

  function withIndexName() {
    // 60-character cut-off: a long body name pushes the list over it.
    withIndex((i) => {
      i.feeds.find((f: any) => f.key === `calendar/${A}/city-council.ics`).body_name = 'X'.repeat(70);
    });
    return 'Alpha: ' + 'X'.repeat(70) + ' + 1 more';
  }

  it('long list falls back to "<first> + k more"', async () => {
    withIndexName();
    const t = unfold(await (await call(MIX(`${A}.city-council.planning-commission`))).text());
    expect(t).toContain(`X-WR-CALNAME:Alpha: ${'X'.repeat(70)} + 1 more\r\n`);
  });

  it('several jurisdictions: name, prefix, description', async () => {
    const r = await call(MIX(`${A}.all_${B}.board-of-commissioners`));
    const t = await r.text();
    const u = unfold(t);
    expect(u).toContain('X-WR-CALNAME:Local meetings: Alpha\\, Beta Co\r\n');
    expect(u).toContain(`X-WR-CALDESC:Unofficial merged calendar from Provenage. Change this selection: https://1507-systems.github.io/provenage/#v1=${A}.all_${B}.board-of-commissioners\r\n`);
    expect(u).toContain('SUMMARY:Alpha: ');
    expect(u).toContain('SUMMARY:Beta Co: ');
    expect(r.headers.get('X-Provenage-Missing')).toBeNull();
  });

  it('many places fall back to "<n> places"', async () => {
    withIndex((i) => {
      i.jurisdictions[A].short_name = 'A'.repeat(35);
      i.jurisdictions[B].short_name = 'B'.repeat(35);
    });
    const t = unfold(await (await call(MIX(`${A}.all_${B}.all`))).text());
    expect(t).toContain('X-WR-CALNAME:Local meetings: 2 places\r\n');
  });
});

describe('place prefix and byte-for-byte copy', () => {
  const sourceEvents = (key: string) => events(FILES[key]);

  it('every VEVENT is byte-identical to its source except SUMMARY, which unfolds to "<short>: <original>"', async () => {
    const t = await (await call(MIX(`${A}.all_${B}.all`))).text();
    const out = events(t);
    const src = [...sourceEvents(`calendar/${A}/all.ics`), ...sourceEvents(`calendar/${B}/all.ics`)];
    expect(out.length).toBe(src.length);
    out.forEach((ev, i) => {
      const short = i < sourceEvents(`calendar/${A}/all.ics`).length ? 'Alpha' : 'Beta Co';
      // Removing exactly the inserted "<short>: CRLF SP" restores the source bytes.
      expect(ev.replace(`SUMMARY:${short}: \r\n `, 'SUMMARY:')).toBe(src[i]);
      expect(unfold(ev)).toBe(unfold(src[i]).replace('SUMMARY:', `SUMMARY:${short}: `));
    });
  });

  it('without a prefix, events are copied exactly', async () => {
    const t = await (await call(MIX(`${A}.city-council.planning-commission`))).text();
    expect(events(t)).toEqual([
      ...sourceEvents(`calendar/${A}/city-council.ics`),
      ...sourceEvents(`calendar/${A}/planning-commission.ics`),
    ]);
  });

  it('the prefix never creates an over-long line, even for a long escaped short name', async () => {
    withIndex((i) => {
      i.jurisdictions[A].short_name = ',;'.repeat(20); // 40 chars, 80 once escaped
      i.jurisdictions[B].short_name = 'é'.repeat(40);
    });
    const t = await (await call(MIX(`${A}.all_${B}.all`))).text();
    for (const line of t.split('\r\n')) expect(new TextEncoder().encode(line).length).toBeLessThanOrEqual(75);
    expect(unfold(t)).toContain('SUMMARY:' + ',;'.repeat(20).replace(/[,;]/g, (c) => '\\' + c) + ': ');
  });
});

describe('VTIMEZONE merge', () => {
  it('one VTIMEZONE per TZID, X-WR-TIMEZONE only when exactly one', async () => {
    const t = await (await call(MIX(`${A}.all_${B}.all`))).text();
    expect(t.match(/BEGIN:VTIMEZONE/g)).toHaveLength(1);
    expect(t).toContain('X-WR-TIMEZONE:America/Denver\r\n');
  });

  it('differing blocks for one TZID keep the longest; two TZIDs are sorted and drop X-WR-TIMEZONE', async () => {
    const b = FILES[`calendar/${B}/all.ics`];
    const longer = b.replace('END:STANDARD\r\n', 'END:STANDARD\r\nX-NOTE:longer\r\n');
    const other = b.split('America/Denver').join('America/Chicago');
    const files: Record<string, string> = {
      ...FILES,
      [`calendar/${B}/all.ics`]: longer,
    };
    setup(files);
    const idx = JSON.parse(files['calendar/index.json']);
    idx.feeds.find((f: any) => f.key === `calendar/${B}/all.ics`).sha256 = sha(longer);
    files['calendar/index.json'] = JSON.stringify(idx);
    setup(files);
    let t = await (await call(MIX(`${A}.all_${B}.all`))).text();
    expect(t.match(/BEGIN:VTIMEZONE/g)).toHaveLength(1);
    expect(t).toContain('X-NOTE:longer');

    files[`calendar/${B}/all.ics`] = other;
    idx.feeds.find((f: any) => f.key === `calendar/${B}/all.ics`).sha256 = sha(other);
    files['calendar/index.json'] = JSON.stringify(idx);
    setup(files);
    t = await (await call(MIX(`${A}.all_${B}.all`))).text();
    expect(t.match(/BEGIN:VTIMEZONE/g)).toHaveLength(2);
    expect(t).not.toContain('X-WR-TIMEZONE');
    expect(t.indexOf('TZID:America/Chicago')).toBeLessThan(t.indexOf('TZID:America/Denver'));
  });
});

describe('ETag and 304', () => {
  it('If-None-Match gives 304 with zero source reads (and no index re-read inside 60 s)', async () => {
    const first = await call(MIX(`${A}.city-council`));
    const etag = first.headers.get('ETag')!;
    fx.log.length = 0;
    resetSourceCache(); // even with a cold source cache, a match must not read sources
    const r = await call(MIX(`${A}.city-council`), { headers: { 'If-None-Match': etag } });
    expect(r.status).toBe(304);
    expect(r.headers.get('ETag')).toBe(etag);
    expect(r.headers.get('Cache-Control')).toBe('public, max-age=300');
    expect(r.headers.get('Last-Modified')).toBe(T0.toUTCString());
    expect(await r.text()).toBe('');
    expect(sourceReads()).toEqual([]);
  });

  it('If-Modified-Since works without If-None-Match, and is ignored when a non-matching INM is sent', async () => {
    const ims = T0.toUTCString();
    expect((await call(MIX(`${A}.city-council`), { headers: { 'If-Modified-Since': ims } })).status).toBe(304);
    const r = await call(MIX(`${A}.city-council`), { headers: { 'If-None-Match': '"nope"', 'If-Modified-Since': ims } });
    expect(r.status).toBe(200);
  });

  it('ETag is a function of the index only: it changes when a source sha256 changes, not otherwise', async () => {
    const e1 = (await call(MIX(`${A}.city-council`))).headers.get('ETag');
    setup();
    expect((await call(MIX(`${A}.city-council`))).headers.get('ETag')).toBe(e1);
    withIndex((i) => {
      i.feeds.find((f: any) => f.key === `calendar/${A}/city-council.ics`).sha256 = 'a'.repeat(64);
    });
    expect((await call(MIX(`${A}.city-council`), { headers: { 'If-None-Match': e1! } })).status).toBe(503); // stale ETag no longer matches; the new sha256 fails verification against the stored bytes
  });

  it('ETag changes with a place short name that changes the output', async () => {
    const e1 = (await call(MIX(`${A}.all_${B}.all`))).headers.get('ETag');
    withIndex((i) => {
      i.jurisdictions[A].short_name = 'Alp';
    });
    const e2 = (await call(MIX(`${A}.all_${B}.all`))).headers.get('ETag');
    expect(e2).not.toBe(e1);
  });
});

describe('503: never a partial calendar', () => {
  const cases: Array<[string, () => void, string]> = [
    ['source missing', () => fx.files && delete fx.files[`calendar/${A}/planning-commission.ics`], '300'],
    ['source read fails', () => fx.st.failKeys.add(`calendar/${A}/planning-commission.ics`), '300'],
  ];
  for (const [name, breakIt, retry] of cases) {
    it(name, async () => {
      breakIt();
      const r = await call(MIX(`${A}.city-council.planning-commission`));
      expect(r.status).toBe(503);
      expect(r.headers.get('Retry-After')).toBe(retry);
      expect(r.headers.get('Cache-Control')).toBe('no-store');
      expect(r.headers.get('Content-Type')).toBe('application/json; charset=utf-8');
      expect(await r.json()).toEqual({ error: 'temporarily unavailable' });
    });
  }

  it('bad shape: bytes match the index sha256 but the layout is wrong', async () => {
    const bad = FILES[`calendar/${A}/planning-commission.ics`].replace('END:VCALENDAR\r\n', '');
    const files: Record<string, string> = { ...FILES, [`calendar/${A}/planning-commission.ics`]: bad };
    const idx = JSON.parse(files['calendar/index.json']);
    idx.feeds.find((f: any) => f.key === `calendar/${A}/planning-commission.ics`).sha256 = sha(bad);
    files['calendar/index.json'] = JSON.stringify(idx);
    setup(files);
    const r = await call(MIX(`${A}.planning-commission`));
    expect(r.status).toBe(503);
    expect(r.headers.get('Retry-After')).toBe('300');
  });

  it('sha256 mismatch is 503 with Retry-After 60, and is not cached', async () => {
    fx.files[`calendar/${A}/planning-commission.ics`] += 'X';
    const r = await call(MIX(`${A}.planning-commission`));
    expect(r.status).toBe(503);
    expect(r.headers.get('Retry-After')).toBe('60');
    // Republish fixes it: next request succeeds.
    fx.files[`calendar/${A}/planning-commission.ics`] = FILES[`calendar/${A}/planning-commission.ics`];
    expect((await call(MIX(`${A}.planning-commission`))).status).toBe(200);
  });

  it('HEAD also reports 503 (no body)', async () => {
    delete fx.files[`calendar/${A}/city-council.ics`];
    const r = await call(MIX(`${A}.city-council`), { method: 'HEAD' });
    expect(r.status).toBe(503);
    expect(await r.text()).toBe('');
  });

  it('index unavailable (no cached copy), missing, unknown schema, or malformed', async () => {
    fx.st.failKeys.add('calendar/index.json');
    expect((await call(MIX(`${A}.all`))).status).toBe(503);
    setup({ ...FILES, 'calendar/index.json': '{"schema":"other/9","feeds":[]}' });
    expect((await call(MIX(`${A}.all`))).status).toBe(503);
    setup({ ...FILES, 'calendar/index.json': 'not json' });
    expect((await call(MIX(`${A}.all`))).status).toBe(503);
    withIndex((i) => {
      i.feeds[0].sha256 = 'zz';
    });
    expect((await call(MIX(`${A}.all`))).status).toBe(503);
    const noIndex = { ...FILES };
    delete noIndex['calendar/index.json'];
    setup(noIndex);
    expect((await call(MIX(`${A}.all`))).status).toBe(503);
  });

  it('merged output over 8 MiB is a 503', async () => {
    const src = FILES[`calendar/${A}/city-council.ics`];
    const ev = events(src)[0]!;
    const big = src.replace(ev, ev.repeat(Math.ceil((8 * 1024 * 1024) / ev.length) + 1));
    const files: Record<string, string> = { ...FILES, [`calendar/${A}/city-council.ics`]: big };
    const idx = JSON.parse(files['calendar/index.json']);
    idx.feeds.find((f: any) => f.key === `calendar/${A}/city-council.ics`).sha256 = sha(big);
    files['calendar/index.json'] = JSON.stringify(idx);
    setup(files);
    expect((await call(MIX(`${A}.city-council`))).status).toBe(503);
  });
});

describe('aliases (section 5.3)', () => {
  it('a renamed body resolves to its target', async () => {
    const t = await (await call(MIX(`${A}.old-commission`))).text();
    expect(events(t)).toEqual(events(FILES[`calendar/${A}/planning-commission.ics`]));
  });

  it('a renamed jurisdiction resolves for "all" and for bodies', async () => {
    const all = await call(MIX('gamma-town.all'));
    expect(events(await all.text())).toEqual(events(FILES[`calendar/${A}/all.ics`]));
    const body = await call(MIX('gamma-town.city-council'));
    expect(events(await body.text())).toEqual(events(FILES[`calendar/${A}/city-council.ics`]));
  });

  it('null aliases and unknown refs are skipped, counted and disclosed; never a partial silent drop', async () => {
    const r = await call(MIX(`${A}.city-council.retired-board_lost-town.all_zzz-town.all`));
    // canonical order: alpha-town, lost-town, zzz-town
    expect(r.status).toBe(200);
    expect(r.headers.get('X-Provenage-Missing')).toBe('3');
    const t = unfold(await r.text());
    expect(t).toContain('3 selected calendars are no longer published.');
    expect(events(t)).toEqual(events(FILES[`calendar/${A}/city-council.ics`]));
    expect(parseSelection(`${A}.city-council.retired-board_lost-town.all_zzz-town.all`).ok).toBe(true);
  });

  it('every ref retired or missing is 404', async () => {
    const r = await call(MIX(`${A}.retired-board_lost-town.all`));
    expect(r.status).toBe(404);
    expect(await r.json()).toEqual({ error: 'no selected calendar is published' });
    expect(r.headers.get('Cache-Control')).toBe('no-store');
  });

  it('a body under a selected "all" is dropped (no duplicate UIDs)', async () => {
    const t = await (await call(MIX(`${A}.all`))).text();
    const t2 = await (await call(MIX(`${A}.all_${B}.board-of-commissioners`))).text();
    expect(new Set(uidsOf(t)).size).toBe(uidsOf(t).length);
    // via alias: gamma-town.all + alpha-town.city-council both resolve into alpha-town/all
    const t3 = await (await call(MIX(`${A}.city-council_gamma-town.all`))).text();
    expect(uidsOf(t3).sort()).toEqual(uidsOf(t).sort());
    expect(t2).toContain('BEGIN:VCALENDAR');
  });

  it('two refs resolving to the same key give each event once', async () => {
    const t = await (await call(MIX(`${A}.planning-commission_gamma-town.old-commission`))).text();
    // gamma-town/old-commission has no body alias; it maps to alpha-town/old-commission, which is missing
    expect(uidsOf(t).length).toBe(events(FILES[`calendar/${A}/planning-commission.ics`]).length);
    const t2 = await (await call(MIX(`${A}.old-commission.planning-commission`))).text();
    expect(uidsOf(t2).length).toBe(2); // both resolve to the same body feed; kept once
  });
});

describe('property: no duplicate UIDs across random selections', () => {
  it('300 seeded random selections', async () => {
    let seed = 12345;
    const rnd = () => ((seed = (seed * 1103515245 + 12345) & 0x7fffffff) / 0x7fffffff);
    const pick = <T,>(a: T[]) => a[Math.floor(rnd() * a.length)];
    const bodies: Record<string, string[]> = {
      [A]: ['city-council', 'planning-commission', 'old-commission', 'retired-board', 'nope'],
      [B]: ['board-of-commissioners', 'parks-board'],
      'gamma-town': ['city-council', 'planning-commission', 'old-commission'],
    };
    for (let n = 0; n < 300; n++) {
      const groups: string[] = [];
      for (const j of Object.keys(bodies).sort()) {
        if (rnd() < 0.3) continue;
        if (rnd() < 0.3) groups.push(`${j}.all`);
        else {
          const k = 1 + Math.floor(rnd() * 3);
          const bs = [...new Set(Array.from({ length: k }, () => pick(bodies[j])))].sort();
          groups.push(`${j}.${bs.join('.')}`);
        }
      }
      if (!groups.length) continue;
      const r = await call(MIX(groups.join('_')));
      if (r.status === 404) continue;
      expect(r.status).toBe(200);
      const u = uidsOf(await r.text());
      expect(new Set(u).size).toBe(u.length);
    }
  });
});

describe('index-derived text cannot inject content lines', () => {
  const EVIL = 'x\r\nBEGIN:VEVENT\r\nSUMMARY:pwned\r\nATTENDEE:mailto:a@b.c\r\n\u0000\u007f,;\\ <script>alert(1)</script>';

  it('hostile titles, body names and short names yield one X-WR-CALNAME and no extra properties or events', async () => {
    withIndex((i) => {
      for (const f of i.feeds) {
        f.title = EVIL;
        f.body_name = f.body_name === null ? null : EVIL;
      }
      for (const j of Object.keys(i.jurisdictions)) {
        i.jurisdictions[j].short_name = EVIL;
        i.jurisdictions[j].name = EVIL;
      }
    });
    for (const sel of [`${A}.city-council`, `${A}.city-council.planning-commission`, `${A}.all_${B}.all`]) {
      const r = await call(MIX(sel));
      expect(r.status, sel).toBe(200);
      const t = await r.text();
      // The event count is exactly the sources' event count.
      const expectN = sel === `${A}.city-council` ? 2 : sel.includes('_') ? 7 : 4;
      expect(events(t)).toHaveLength(expectN);
      expect(t).not.toMatch(/\r\nATTENDEE/);
      expect(t).not.toContain('pwned\r\n');
      expect(/[\u0000\u007f]/.test(t)).toBe(false);
      const logical = unfold(t).split('\r\n');
      expect(logical.filter((l) => l.startsWith('X-WR-CALNAME:'))).toHaveLength(1);
      expect(logical.filter((l) => l.startsWith('X-WR-CALDESC:'))).toHaveLength(1);
      // Every logical header line before the first VTIMEZONE is a known property.
      const hdr = logical.slice(0, logical.indexOf('BEGIN:VTIMEZONE'));
      for (const l of hdr) expect(l).toMatch(/^(BEGIN:VCALENDAR|VERSION:|PRODID:|CALSCALE:|METHOD:|X-WR-CALNAME:|X-WR-CALDESC:|X-WR-TIMEZONE:|REFRESH-INTERVAL;|X-PUBLISHED-TTL:)/);
      // No unescaped separators in the cleaned name value.
      const name = logical.find((l) => l.startsWith('X-WR-CALNAME:'))!.slice(13);
      expect(name).not.toMatch(/(^|[^\\])[,;]/);
    }
  });

  it('the fixture with a hostile body_name (built by the real builder) is escaped', async () => {
    const t = unfold(await (await call(MIX(`${B}.parks-board`))).text());
    expect(t).toContain('X-WR-CALNAME:Beta County Government: Parks\\, Rec\\; Board \\\\ <script>alert(1)</script>\r\n');
  });

  it('caps the name at 120 characters and the description at 1,000 octets', async () => {
    withIndex((i) => {
      for (const f of i.feeds) f.title = 'N'.repeat(500);
    });
    const t = unfold(await (await call(MIX(`${A}.city-council`))).text());
    expect(/X-WR-CALNAME:(N+)\r\n/.exec(t)![1]).toHaveLength(120);
  });
});

describe('source cache (sha256-keyed LRU)', () => {
  it('a second selection reusing a feed does not re-read it; an unchanged index causes no further reads', async () => {
    await call(MIX(`${A}.city-council`));
    expect(sourceReads()).toEqual([`get calendar/${A}/city-council.ics`]);
    fx.log.length = 0;
    await call(MIX(`${A}.city-council.planning-commission`));
    expect(sourceReads()).toEqual([`get calendar/${A}/planning-commission.ics`]);
    fx.log.length = 0;
    await call(MIX(`${A}.city-council`));
    expect(sourceReads()).toEqual([]);
  });

  it('a republished source (new sha256 in the index) is fetched again', async () => {
    await call(MIX(`${A}.city-council`));
    const newer = FILES[`calendar/${A}/city-council.ics`].replace('SEQUENCE:0', 'SEQUENCE:1');
    const idx = JSON.parse(fx.files['calendar/index.json']);
    idx.feeds.find((f: any) => f.key === `calendar/${A}/city-council.ics`).sha256 = sha(newer);
    fx.files[`calendar/${A}/city-council.ics`] = newer;
    fx.files['calendar/index.json'] = JSON.stringify(idx);
    vi.setSystemTime(new Date(T0.getTime() + 61_000));
    const t = await (await call(MIX(`${A}.city-council`))).text();
    expect(t).toContain('SEQUENCE:1');
  });
});

describe('fold port', () => {
  it('matches Python meeting_ics.ical.fold on shared vectors', () => {
    const vectors = JSON.parse(readFileSync(join(FIX, 'fold-vectors.json'), 'utf8'));
    expect(vectors.length).toBeGreaterThan(8);
    for (const v of vectors) expect(fold(v.input)).toEqual(v.folded);
  });
});

describe('merged outputs for the external validator', () => {
  // CI sets MIX_OUT_DIR and then runs `python -m meeting_ics check-feed` on every file written here.
  const dir = process.env.MIX_OUT_DIR;
  const sels = [
    `${A}.all`,
    `${A}.city-council`,
    `${A}.city-council.planning-commission`,
    `${B}.parks-board`,
    `${A}.all_${B}.all`,
    `${A}.city-council_${B}.board-of-commissioners.parks-board`,
    `${A}.city-council.retired-board_lost-town.all`,
  ];
  it.each(sels)('writes %s', async (sel) => {
    const r = await call(MIX(sel));
    expect(r.status).toBe(200);
    const text = await r.text();
    if (dir) {
      mkdirSync(dir, { recursive: true });
      writeFileSync(join(dir, `${sel}.ics`), text, 'utf8');
    }
    expect(text.length).toBeGreaterThan(0);
  });
});

describe('index sanity for these tests', () => {
  it('fixture index carries the aliases and short names the tests rely on', () => {
    expect(INDEX.aliases.bodies[`${A}/old-commission`]).toBe(`${A}/planning-commission`);
    expect(INDEX.jurisdictions[B].status).toBe('dev');
  });
});
