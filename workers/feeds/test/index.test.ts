import { describe, it, expect } from 'vitest';
import worker, { type Env } from '../src/index';

const ICS = 'BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n';

function fakeBucket(files: Record<string, string>): R2Bucket {
  const meta = (key: string) => ({
    key,
    size: files[key].length,
    httpEtag: `"etag-${key.length}"`,
    uploaded: new Date('2026-06-01T12:00:00Z'),
  });
  return {
    async get(key: string) {
      if (!(key in files)) return null;
      return { ...meta(key), body: new Response(files[key]).body };
    },
    async head(key: string) {
      return key in files ? meta(key) : null;
    },
    async list() {
      return { objects: Object.keys(files).map(meta), truncated: false };
    },
  } as unknown as R2Bucket;
}

const env: Env = {
  FEEDS: fakeBucket({
    'calendar/longmont-co/city-council.ics': ICS,
    'calendar/longmont-co/city-council.json': '{"title":"City Council"}',
    'calendar/index.json': '{"feeds":[]}',
  }),
};
const call = (path: string, init?: RequestInit) =>
  worker.fetch(new Request(`https://x.test${path}`, init), env);

describe('feed worker', () => {
  it('200 serves ics with headers', async () => {
    const r = await call('/calendar/longmont-co/city-council.ics');
    expect(r.status).toBe(200);
    expect(r.headers.get('Content-Type')).toBe('text/calendar; charset=utf-8');
    expect(r.headers.get('ETag')).toBeTruthy();
    expect(r.headers.get('Last-Modified')).toBe('Mon, 01 Jun 2026 12:00:00 GMT');
    expect(r.headers.get('Cache-Control')).toBe('public, max-age=300');
    expect(r.headers.get('X-Content-Type-Options')).toBe('nosniff');
    expect(await r.text()).toBe(ICS);
  });

  it('304 on matching If-None-Match', async () => {
    const first = await call('/calendar/longmont-co/city-council.ics');
    const r = await call('/calendar/longmont-co/city-council.ics', {
      headers: { 'If-None-Match': first.headers.get('ETag')! },
    });
    expect(r.status).toBe(304);
    expect(await r.text()).toBe('');
  });

  it('404 JSON for missing and unknown paths', async () => {
    for (const p of ['/calendar/longmont-co/nope.ics', '/', '/calendar/a/b/c.ics', '/calendar/../x.ics']) {
      const r = await call(p);
      expect(r.status).toBe(404);
      expect(r.headers.get('Content-Type')).toContain('application/json');
    }
  });

  it('HEAD has headers and no body', async () => {
    const r = await call('/calendar/longmont-co/city-council.ics', { method: 'HEAD' });
    expect(r.status).toBe(200);
    expect(r.headers.get('ETag')).toBeTruthy();
    expect(await r.text()).toBe('');
  });

  it('serves json sidecar and index', async () => {
    expect((await call('/calendar/longmont-co/city-council.json')).status).toBe(200);
    expect(await (await call('/calendar/index.json')).text()).toBe('{"feeds":[]}');
  });

  it('304 on If-Modified-Since at or after upload, 200 before', async () => {
    const p = '/calendar/longmont-co/city-council.ics';
    expect((await call(p, { headers: { 'If-Modified-Since': 'Mon, 01 Jun 2026 12:00:00 GMT' } })).status).toBe(304);
    expect((await call(p, { headers: { 'If-Modified-Since': 'Mon, 01 Jun 2026 11:59:59 GMT' } })).status).toBe(200);
  });

  it('HEAD 404 has no body', async () => {
    const r = await call('/calendar/longmont-co/nope.ics', { method: 'HEAD' });
    expect(r.status).toBe(404);
    expect(await r.text()).toBe('');
  });

  it('rejects other methods', async () => {
    const r = await call('/calendar/longmont-co/city-council.ics', { method: 'POST' });
    expect(r.status).toBe(405);
  });
});
