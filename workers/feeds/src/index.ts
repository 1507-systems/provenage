/**
 * provenage-feeds: public, read-only iCalendar feed server.
 *
 * Mounted behind the shared API host router with strip-prefix, so every path here is
 * RELATIVE to the mount. The mount slug is deliberately absent from this
 * file: it lives only in the router config and the publish script env.
 *
 * No auth, no RSVP/attendance/member features by design.
 */

export interface Env {
  FEEDS: R2Bucket;
}

const SEGMENT = '[a-z0-9][a-z0-9._-]{0,63}';
const FEED_RE = new RegExp(`^/calendar/(${SEGMENT})/(${SEGMENT})\\.(ics|json)$`);
const INDEX_PATH = '/calendar/index.json';
const INDEX_KEY = 'calendar/index.json';

function baseHeaders(extra: Record<string, string> = {}): Headers {
  const h = new Headers(extra);
  h.set('X-Content-Type-Options', 'nosniff');
  return h;
}

function notFound(isHead = false): Response {
  return new Response(isHead ? null : JSON.stringify({ error: 'not found' }), {
    status: 404,
    headers: baseHeaders({ 'Content-Type': 'application/json; charset=utf-8' }),
  });
}

function methodNotAllowed(): Response {
  return new Response(JSON.stringify({ error: 'method not allowed' }), {
    status: 405,
    headers: baseHeaders({
      'Content-Type': 'application/json; charset=utf-8',
      Allow: 'GET, HEAD',
    }),
  });
}

function etagMatches(header: string | null, etag: string): boolean {
  if (!header) return false;
  if (header.trim() === '*') return true;
  const weak = (s: string) => s.trim().replace(/^W\//, '');
  return header.split(',').some((t) => weak(t) === weak(etag));
}

async function serveObject(
  request: Request,
  env: Env,
  key: string,
  contentType: string,
): Promise<Response> {
  const isHead = request.method === 'HEAD';
  const obj = isHead ? await env.FEEDS.head(key) : await env.FEEDS.get(key);
  if (!obj) return notFound(isHead);

  const headers = baseHeaders({
    'Content-Type': contentType,
    ETag: obj.httpEtag,
    'Last-Modified': obj.uploaded.toUTCString(),
    'Cache-Control': 'public, max-age=300',
  });

  const inm = request.headers.get('If-None-Match');
  if (etagMatches(inm, obj.httpEtag)) {
    return new Response(null, { status: 304, headers });
  }
  // RFC 9110 §13.1.3: If-Modified-Since is ignored when If-None-Match is present.
  const ims = inm ? null : request.headers.get('If-Modified-Since');
  if (ims) {
    const since = Date.parse(ims);
    // HTTP dates have 1 s resolution; compare at that granularity.
    if (!Number.isNaN(since) && Math.floor(obj.uploaded.getTime() / 1000) * 1000 <= since) {
      return new Response(null, { status: 304, headers });
    }
  }
  if (isHead) {
    headers.set('Content-Length', String(obj.size));
    return new Response(null, { status: 200, headers });
  }
  return new Response((obj as R2ObjectBody).body, { status: 200, headers });
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    if (request.method !== 'GET' && request.method !== 'HEAD') return methodNotAllowed();

    const { pathname } = new URL(request.url);

    if (pathname === INDEX_PATH) {
      return serveObject(request, env, INDEX_KEY, 'application/json; charset=utf-8');
    }

    const m = FEED_RE.exec(pathname);
    // SEGMENT requires a leading [a-z0-9], so '.', '..' and encoded forms never match.
    if (!m) return notFound(request.method === 'HEAD');
    const [, jurisdiction, body, ext] = m;
    const key = `calendar/${jurisdiction}/${body}.${ext}`;
    const type = ext === 'ics' ? 'text/calendar; charset=utf-8' : 'application/json; charset=utf-8';
    return serveObject(request, env, key, type);
  },
} satisfies ExportedHandler<Env>;
