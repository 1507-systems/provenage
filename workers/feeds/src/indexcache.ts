/**
 * In-memory cache of the feed index (calendar/index.json), one copy per isolate.
 *
 * - Revalidated against R2 at most once per RECHECK_MS, using a conditional read
 *   (etagDoesNotMatch), so an unchanged index costs a cheap metadata-only read.
 * - Concurrent callers share one in-flight read.
 * - If R2 fails, the last good copy is served for up to STALE_MS after it was last
 *   confirmed good; after that the caller gets an error (null result, ok=false).
 * - Failed rechecks are retried no sooner than RECHECK_MS later, so an R2 outage
 *   is not hammered by request bursts.
 *
 * Nothing here logs anything (privacy: see wrangler.toml observability note).
 */

export const INDEX_KEY = 'calendar/index.json';
export const RECHECK_MS = 60_000;
export const STALE_MS = 3_600_000;

export interface IndexCopy {
  text: string;
  httpEtag: string;
  uploaded: Date;
  size: number;
}

export type IndexResult =
  | { ok: true; copy: IndexCopy | null } // null: the index object does not exist
  | { ok: false }; // unavailable and no usable cached copy

interface State {
  copy: IndexCopy;
  checkedAt: number; // last recheck attempt (success or failure)
  goodAt: number; // last time R2 confirmed this copy
}

let state: State | null = null;
let inflight: Promise<IndexResult> | null = null;

/** Test hook: forget the cached copy. */
export function resetIndexCache(): void {
  state = null;
  inflight = null;
}

async function refresh(bucket: R2Bucket, now: number): Promise<IndexResult> {
  try {
    const prev = state;
    const obj = await bucket.get(
      INDEX_KEY,
      prev ? { onlyIf: { etagDoesNotMatch: prev.copy.httpEtag } } : undefined,
    );
    if (!obj) {
      state = null;
      return { ok: true, copy: null };
    }
    if (!('body' in obj) || !obj.body) {
      // Precondition failed: our copy is still current.
      if (prev) {
        prev.checkedAt = now;
        prev.goodAt = now;
        return { ok: true, copy: prev.copy };
      }
      throw new Error('no body');
    }
    const text = await new Response((obj as R2ObjectBody).body).text();
    const copy: IndexCopy = {
      text,
      httpEtag: obj.httpEtag,
      uploaded: obj.uploaded,
      size: obj.size,
    };
    state = { copy, checkedAt: now, goodAt: now };
    return { ok: true, copy };
  } catch {
    const s = state;
    if (s) {
      s.checkedAt = now; // back off before the next attempt
      if (now - s.goodAt <= STALE_MS) return { ok: true, copy: s.copy };
      state = null;
    }
    return { ok: false };
  }
}

export function getIndex(bucket: R2Bucket): Promise<IndexResult> {
  const now = Date.now();
  const s = state;
  if (s && now - s.checkedAt < RECHECK_MS && now >= s.checkedAt) {
    return Promise.resolve({ ok: true, copy: s.copy });
  }
  if (!inflight) {
    inflight = refresh(bucket, now).finally(() => {
      inflight = null;
    });
  }
  return inflight;
}
