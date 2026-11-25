import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import worker, { type Env } from '../src/index';
import { resetIndexCache } from '../src/indexcache';

const ICS = 'BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n';
const T0 = new Date('2026-06-01T12:00:00Z');

/** Fake R2 that logs every call, honours onlyIf.etagDoesNotMatch, and can fail on demand. */
function makeBucket(files: Record<string, string>) {
  const log: string[] = [];
  const state = { fail: false, etagN: 1 };
  const meta = (key: string) => ({
    key,
    size: files[key].length,
    etag: `e${state.etagN}-${key.length}`,
    httpEtag: `"e${state.etagN}-${key.length}"`,
    uploaded: new Date('2026-06-01T12:00:00Z'),
  });
  const bucket = {
    async get(key: string, opts?: { onlyIf?: { etagDoesNotMatch?: string } }) {
      log.push(`get ${key}${opts?.onlyIf ? ' cond' : ''}`);
      if (state.fail) throw new Error('r2 down');
      const cond = opts?.onlyIf?.etagDoesNotMatch;
      // Real workerd rejects quoted conditional ETags.
      if (cond?.startsWith('"')) {
        throw new TypeError(`Conditional ETag should not be wrapped in quotes ("${cond}")`);
      }
      if (!(key in files)) return null;
      const m = meta(key);
      if (cond === m.etag) return m; // no body
      return {
        ...m,
        body: new Response(files[key]).body,
        text: async () => files[key],
      };
    },
    async head(key: string) {
      log.push(`head ${key}`);
      return key in files ? meta(key) : null;
    },
  } as unknown as R2Bucket;
  return { bucket, log, state, files };
}

const INDEX = '{"feeds":[],"schema":"provenage-calendar-index/1"}';
let fx: ReturnType<typeof makeBucket>;
let env: Env;
const call = (path: string, init?: RequestInit) =>
  worker.fetch(new Request(`https://x.test${path}`, init), env);
const indexReads = () => fx.log.filter((l) => l.includes('calendar/index.json')).length;

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(T0);
  resetIndexCache();
  fx = makeBucket({
    'calendar/index.json': INDEX,
    'calendar/demo-town/all.ics': ICS,
  });
  env = { FEEDS: fx.bucket };
});
afterEach(() => vi.useRealTimers());

describe('CORS', () => {
  it('present on index 200, 304 and HEAD', async () => {
    const r = await call('/calendar/index.json');
    expect(r.status).toBe(200);
    expect(r.headers.get('Access-Control-Allow-Origin')).toBe('*');
    expect(await r.text()).toBe(INDEX);
    const r304 = await call('/calendar/index.json', { headers: { 'If-None-Match': r.headers.get('ETag')! } });
    expect(r304.status).toBe(304);
    expect(r304.headers.get('Access-Control-Allow-Origin')).toBe('*');
    const h = await call('/calendar/index.json', { method: 'HEAD' });
    expect(h.status).toBe(200);
    expect(h.headers.get('Access-Control-Allow-Origin')).toBe('*');
    expect(h.headers.get('Content-Length')).toBe(String(INDEX.length));
    expect(await h.text()).toBe('');
  });

  it('absent on .ics, 404, 405 and 503', async () => {
    for (const p of ['/calendar/demo-town/all.ics', '/calendar/nope/x.ics', '/nothing']) {
      for (const method of ['GET', 'HEAD']) {
        const r = await call(p, { method });
        expect(r.headers.get('Access-Control-Allow-Origin'), `${method} ${p}`).toBeNull();
      }
    }
    const opt = await call('/calendar/index.json', { method: 'OPTIONS' });
    expect(opt.status).toBe(405);
    expect(opt.headers.get('Access-Control-Allow-Origin')).toBeNull();
    const post = await call('/calendar/demo-town/all.ics', { method: 'POST' });
    expect(post.headers.get('Access-Control-Allow-Origin')).toBeNull();

    fx.state.fail = true; // no cached copy: 503, no CORS
    const r503 = await call('/calendar/index.json');
    expect(r503.status).toBe(503);
    expect(r503.headers.get('Access-Control-Allow-Origin')).toBeNull();
    expect(r503.headers.get('Cache-Control')).toBe('no-store');
  });

  it('index 404 (object missing) has no CORS', async () => {
    delete fx.files['calendar/index.json'];
    const r = await call('/calendar/index.json');
    expect(r.status).toBe(404);
    expect(r.headers.get('Access-Control-Allow-Origin')).toBeNull();
  });
});

describe('index cache', () => {
  it('burst of 100 requests causes at most one index read per 60 s', async () => {
    await Promise.all(Array.from({ length: 100 }, () => call('/calendar/index.json')));
    expect(indexReads()).toBe(1);
    for (let i = 0; i < 100; i++) await call('/calendar/index.json');
    expect(indexReads()).toBe(1);
    vi.setSystemTime(T0.getTime() + 59_999);
    await call('/calendar/index.json');
    expect(indexReads()).toBe(1);
    vi.setSystemTime(T0.getTime() + 60_000);
    await Promise.all(Array.from({ length: 100 }, () => call('/calendar/index.json')));
    expect(indexReads()).toBe(2);
    // The recheck is conditional.
    expect(fx.log.filter((l) => l.endsWith('cond')).length).toBe(1);
  });

  it('picks up a changed index after the recheck window', async () => {
    await call('/calendar/index.json');
    fx.files['calendar/index.json'] = '{"feeds":[1]}';
    fx.state.etagN = 2;
    expect(await (await call('/calendar/index.json')).text()).toBe(INDEX); // still within 60 s
    vi.setSystemTime(T0.getTime() + 61_000);
    expect(await (await call('/calendar/index.json')).text()).toBe('{"feeds":[1]}');
  });

  it('conditional recheck serves the new body and ETag within one recheck window', async () => {
    const r1 = await call('/calendar/index.json');
    const etag1 = r1.headers.get('ETag');
    fx.files['calendar/index.json'] = '{"feeds":[2]}';
    fx.state.etagN = 2;
    vi.setSystemTime(T0.getTime() + 60_000);
    const r2 = await call('/calendar/index.json');
    expect(await r2.text()).toBe('{"feeds":[2]}');
    expect(r2.headers.get('ETag')).not.toBe(etag1);
    expect(fx.log.filter((l) => l.endsWith('cond')).length).toBe(1);
    expect(indexReads()).toBe(2);
    // Advancing past STALE_MS must not 503: the recheck really succeeded (goodAt advanced).
    vi.setSystemTime(T0.getTime() + 60_000 + 3_600_001);
    expect((await call('/calendar/index.json')).status).toBe(200);
  });

  it('unchanged index: conditional recheck succeeds and keeps goodAt fresh', async () => {
    await call('/calendar/index.json');
    for (let i = 1; i <= 80; i++) {
      vi.setSystemTime(T0.getTime() + i * 60_000);
      expect((await call('/calendar/index.json')).status).toBe(200);
    }
    // Every recheck was conditional and none fell back to the failure path.
    expect(fx.log.filter((l) => l.endsWith('cond')).length).toBe(80);
    fx.state.fail = true;
    vi.setSystemTime(T0.getTime() + 81 * 60_000);
    expect((await call('/calendar/index.json')).status).toBe(200); // goodAt recent
  });

  it('serves the last good copy for up to 1 h when R2 fails, then 503', async () => {
    await call('/calendar/index.json');
    fx.state.fail = true;
    vi.setSystemTime(T0.getTime() + 61_000);
    const r = await call('/calendar/index.json');
    expect(r.status).toBe(200);
    expect(await r.text()).toBe(INDEX);
    // Failed recheck backs off: no new read for another 60 s.
    const n = indexReads();
    for (let i = 0; i < 20; i++) await call('/calendar/index.json');
    expect(indexReads()).toBe(n);
    vi.setSystemTime(T0.getTime() + 3_600_000);
    expect((await call('/calendar/index.json')).status).toBe(200);
    vi.setSystemTime(T0.getTime() + 3_600_001 + 60_000);
    expect((await call('/calendar/index.json')).status).toBe(503);
    fx.state.fail = false;
    vi.setSystemTime(T0.getTime() + 3_600_001 + 180_000);
    expect((await call('/calendar/index.json')).status).toBe(200);
  });

  it('feed routes never touch the index cache', async () => {
    await call('/calendar/demo-town/all.ics');
    expect(indexReads()).toBe(0);
  });
});

describe('privacy', () => {
  it('src/ never calls console.*', () => {
    const files = (import.meta as any).glob('../src/**/*.ts', { query: '?raw', import: 'default', eager: true }) as Record<string, string>;
    expect(Object.keys(files).length).toBeGreaterThan(1);
    for (const [name, text] of Object.entries(files)) {
      expect(text, name).not.toMatch(/console\s*\./);
    }
  });

  it('wrangler.toml keeps observability disabled', () => {
    const files = (import.meta as any).glob('../wrangler.toml', { query: '?raw', import: 'default', eager: true }) as Record<string, string>;
    const text = Object.values(files)[0];
    expect(text).toMatch(/\[observability\]\s*\n(?:#[^\n]*\n)*\s*enabled\s*=\s*false/);
  });
});
