/**
 * Per-isolate LRU of parsed source feeds, keyed by the index's sha256 (content-addressed, so a hit is always
 * the right bytes). Bytes are verified against that sha256 before they are inserted. No Cache API, no logging.
 */

import { parseSource, SourceShapeError, type ParsedSource } from './merge';

export const CACHE_CAP_BYTES = 32 * 1024 * 1024;
export const MAX_SOURCE_BYTES = 8 * 1024 * 1024;

/** Read/verify failure. `mismatch` selects Retry-After 60 instead of 300. */
export class SourceError extends Error {
  constructor(public mismatch: boolean) {
    super('source unavailable');
  }
}

interface Entry {
  parsed: ParsedSource;
  bytes: number;
}

const lru = new Map<string, Entry>();
let total = 0;

export function resetSourceCache(): void {
  lru.clear();
  total = 0;
}

async function sha256Hex(buf: ArrayBuffer): Promise<string> {
  const d = new Uint8Array(await crypto.subtle.digest('SHA-256', buf));
  let s = '';
  for (const b of d) s += b.toString(16).padStart(2, '0');
  return s;
}

function remember(sha: string, e: Entry): void {
  lru.set(sha, e);
  total += e.bytes;
  for (const k of lru.keys()) {
    if (total <= CACHE_CAP_BYTES) break;
    if (k === sha) continue; // never evict what we just inserted
    total -= lru.get(k)!.bytes;
    lru.delete(k);
  }
}

async function load(bucket: R2Bucket, key: string, sha: string): Promise<ParsedSource> {
  const hit = lru.get(sha);
  if (hit) {
    lru.delete(sha); // refresh recency
    lru.set(sha, hit);
    return hit.parsed;
  }
  let buf: ArrayBuffer;
  try {
    const obj = await bucket.get(key);
    if (!obj || !('body' in obj)) throw new SourceError(false);
    buf = await (obj as R2ObjectBody).arrayBuffer();
  } catch {
    throw new SourceError(false);
  }
  if (buf.byteLength > MAX_SOURCE_BYTES) throw new SourceError(false);
  if ((await sha256Hex(buf)) !== sha) throw new SourceError(true);
  let parsed: ParsedSource;
  try {
    parsed = parseSource(new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(buf));
  } catch (e) {
    if (e instanceof SourceShapeError || e instanceof TypeError) throw new SourceError(false);
    throw e;
  }
  remember(sha, { parsed, bytes: buf.byteLength });
  return parsed;
}

/** Load every source in parallel; if any fails, the whole call fails (never a partial calendar). */
export async function loadSources(
  bucket: R2Bucket,
  wanted: Array<{ key: string; sha256: string }>,
): Promise<ParsedSource[]> {
  const results = await Promise.allSettled(wanted.map((w) => load(bucket, w.key, w.sha256)));
  const failed = results.find((r) => r.status === 'rejected') as PromiseRejectedResult | undefined;
  if (failed) {
    const mismatch = results.some((r) => r.status === 'rejected' && r.reason instanceof SourceError && r.reason.mismatch);
    throw new SourceError(mismatch);
  }
  return results.map((r) => (r as PromiseFulfilledResult<ParsedSource>).value);
}
