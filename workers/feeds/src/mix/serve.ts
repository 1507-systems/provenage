/**
 * GET/HEAD /mix/v1/<selection>.ics: one calendar merged from several published feeds.
 *
 * Never an empty or partial calendar on error (clients delete events missing from a 200 but keep their last
 * copy on an error). The ETag depends on the index only, so a conditional request costs no source reads.
 * No logging (see wrangler.toml), no Cache API, no storage access before the URL passes the grammar check.
 */

import { getIndex } from '../indexcache';
import { selectionString, parseMixPath } from './parse';
import { parseIndex, resolveGroups, type FeedEntry } from './resolve';
import { mergeSources, nameDescLines, MAX_OUTPUT_BYTES, type HeaderInput } from './merge';
import { loadSources, SourceError } from './sources';

const JSON_TYPE = 'application/json; charset=utf-8';

function jsonResponse(status: number, body: unknown, isHead: boolean, extra: Record<string, string> = {}): Response {
  const h = new Headers({
    'Content-Type': JSON_TYPE,
    'Cache-Control': 'no-store',
    'X-Content-Type-Options': 'nosniff',
    ...extra,
  });
  return new Response(isHead ? null : JSON.stringify(body), { status, headers: h });
}

const unavailable = (isHead: boolean, retry: number): Response =>
  jsonResponse(503, { error: 'temporarily unavailable' }, isHead, { 'Retry-After': String(retry) });

async function sha256Hex(text: string): Promise<string> {
  const d = new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text)));
  return Array.from(d, (b) => b.toString(16).padStart(2, '0')).join('');
}

function etagMatches(header: string | null, etag: string): boolean {
  if (!header) return false;
  if (header.trim() === '*') return true;
  const weak = (s: string) => s.trim().replace(/^W\//, '');
  return header.split(',').some((t) => weak(t) === weak(etag));
}

export async function serveMix(request: Request, env: { FEEDS: R2Bucket }, pathname: string): Promise<Response> {
  const isHead = request.method === 'HEAD';

  // 1. Grammar, canonical form and limits, before any storage access.
  const parsed = parseMixPath(pathname);
  if (!parsed.ok) {
    return jsonResponse(404, parsed.canonical ? { error: parsed.error, canonical: parsed.canonical } : { error: parsed.error }, isHead);
  }

  // 2. Index (module-scope cache, 60 s revalidation, 1 h stale-on-error).
  const r = await getIndex(env.FEEDS);
  if (!r.ok || !r.copy) return unavailable(isHead, 300);
  const index = parseIndex(r.copy);
  if (!index) return unavailable(isHead, 300);

  // 3. Resolve aliases; header from index data alone.
  const { entries, missing } = resolveGroups(index, parsed.groups);
  if (entries.length === 0) return jsonResponse(404, { error: 'no selected calendar is published' }, isHead);

  const shortOf = (e: FeedEntry) => index.shortNames.get(e.jurisdiction) ?? e.jurisdiction;
  const header: HeaderInput = {
    items: entries.map((e) => ({
      jurisdiction: e.jurisdiction,
      shortName: shortOf(e),
      title: e.title,
      bodyName: e.bodyName,
      combined: e.kind === 'combined',
    })),
    canonicalSelection: selectionString(parsed.groups),
    missing,
  };
  const prefix = new Set(entries.map((e) => e.jurisdiction)).size > 1;

  // 4. ETag from the index alone. When the place prefix applies, each short name is part of the input because
  // it changes the output without changing any source sha256.
  const hashInput =
    `mix/v1\n${parsed.selection}\n${nameDescLines(header)}\n` +
    entries.map((e) => `${e.key} ${e.sha256}${prefix ? ' ' + shortOf(e) : ''}\n`).join('');
  const etag = `"m1-${(await sha256Hex(hashInput)).slice(0, 32)}"`;

  const headers = new Headers({
    'Content-Type': 'text/calendar; charset=utf-8',
    ETag: etag,
    'Last-Modified': r.copy.uploaded.toUTCString(),
    'Cache-Control': 'public, max-age=300',
    'X-Content-Type-Options': 'nosniff',
  });
  if (missing > 0) headers.set('X-Provenage-Missing', String(missing));

  const inm = request.headers.get('If-None-Match');
  const ims = inm ? null : request.headers.get('If-Modified-Since');
  const since = ims ? Date.parse(ims) : NaN;
  if (
    etagMatches(inm, etag) ||
    (!Number.isNaN(since) && Math.floor(r.copy.uploaded.getTime() / 1000) * 1000 <= since)
  ) {
    headers.delete('Content-Type');
    return new Response(null, { status: 304, headers });
  }

  // 5-6. Sources (verified against the index sha256), then merge.
  let body: Uint8Array;
  try {
    const srcs = await loadSources(env.FEEDS, entries);
    const text = mergeSources(
      header,
      srcs.map((parsedSrc, i) => ({ parsed: parsedSrc, shortName: shortOf(entries[i]) })),
      prefix,
    );
    body = new TextEncoder().encode(text);
  } catch (e) {
    return unavailable(isHead, e instanceof SourceError && e.mismatch ? 60 : 300);
  }
  if (body.length > MAX_OUTPUT_BYTES) return unavailable(isHead, 300);

  return new Response(isHead ? null : body, { status: 200, headers });
}
