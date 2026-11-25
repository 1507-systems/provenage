/**
 * Index parsing and alias resolution for /mix/v1/ (resolved spec section 5.3).
 *
 * The parsed index is memoised per IndexCopy object (the copy changes only when the index object changes).
 * Everything read from the index is validated here; a malformed index makes the caller answer 503, never a
 * partial calendar.
 */

import type { IndexCopy } from '../indexcache';
import type { Group } from './parse';

export const INDEX_SCHEMA = 'provenage-calendar-index/1';
const SHA_RE = /^[0-9a-f]{64}$/;

export interface FeedEntry {
  key: string;
  sha256: string;
  jurisdiction: string;
  kind: 'body' | 'combined';
  title: string;
  bodyName: string | null;
}

export interface ParsedIndex {
  feeds: Map<string, FeedEntry>;
  /** slug -> display short name (falls back to name, then slug). */
  shortNames: Map<string, string>;
  bodyAliases: Map<string, string | null>;
  jurisdictionAliases: Map<string, string | null>;
}

const isObj = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v);
const isStr = (v: unknown): v is string => typeof v === 'string';

function aliasMap(raw: unknown): Map<string, string | null> | null {
  const out = new Map<string, string | null>();
  if (raw === undefined) return out;
  if (!isObj(raw)) return null;
  for (const [k, v] of Object.entries(raw)) {
    if (v !== null && !isStr(v)) return null;
    out.set(k, v);
  }
  return out;
}

const memo = new WeakMap<IndexCopy, ParsedIndex | null>();

/** Parse and validate; null means the index is unusable (caller answers 503). */
export function parseIndex(copy: IndexCopy): ParsedIndex | null {
  if (memo.has(copy)) return memo.get(copy)!;
  const parsed = parseIndexText(copy.text);
  memo.set(copy, parsed);
  return parsed;
}

function parseIndexText(text: string): ParsedIndex | null {
  let doc: unknown;
  try {
    doc = JSON.parse(text);
  } catch {
    return null;
  }
  if (!isObj(doc) || doc.schema !== INDEX_SCHEMA || !Array.isArray(doc.feeds)) return null;

  const feeds = new Map<string, FeedEntry>();
  for (const f of doc.feeds) {
    if (
      !isObj(f) || !isStr(f.key) || !isStr(f.sha256) || !SHA_RE.test(f.sha256) || !isStr(f.jurisdiction) ||
      (f.kind !== 'body' && f.kind !== 'combined') || !isStr(f.title) ||
      !(f.body_name === null || f.body_name === undefined || isStr(f.body_name))
    ) {
      return null;
    }
    feeds.set(f.key, {
      key: f.key,
      sha256: f.sha256,
      jurisdiction: f.jurisdiction,
      kind: f.kind,
      title: f.title,
      bodyName: isStr(f.body_name) ? f.body_name : null,
    });
  }

  const shortNames = new Map<string, string>();
  if (doc.jurisdictions !== undefined) {
    if (!isObj(doc.jurisdictions)) return null;
    for (const [slug, j] of Object.entries(doc.jurisdictions)) {
      if (!isObj(j)) return null;
      const v = isStr(j.short_name) ? j.short_name : isStr(j.name) ? j.name : slug;
      shortNames.set(slug, v);
    }
  }

  const aliases = doc.aliases === undefined ? {} : doc.aliases;
  if (!isObj(aliases)) return null;
  const bodyAliases = aliasMap(aliases.bodies);
  const jurisdictionAliases = aliasMap(aliases.jurisdictions);
  if (!bodyAliases || !jurisdictionAliases) return null;

  return { feeds, shortNames, bodyAliases, jurisdictionAliases };
}

export interface Resolved {
  /** Feed entries to merge, in resolved order, duplicates and shadowed body feeds already dropped. */
  entries: FeedEntry[];
  /** Refs that were retired (null alias) or are not in the index. */
  missing: number;
}

/** Keys come from validated slugs only, so plain concatenation is safe. */
export function resolveGroups(index: ParsedIndex, groups: Group[]): Resolved {
  let missing = 0;
  const picked: FeedEntry[] = [];
  const seen = new Set<string>();

  const take = (key: string | null) => {
    const entry = key === null ? undefined : index.feeds.get(key);
    if (!entry) {
      missing++;
      return;
    }
    if (seen.has(entry.key)) return; // duplicate after alias resolution: first kept
    seen.add(entry.key);
    picked.push(entry);
  };

  const mapJ = (j: string): string | null =>
    index.jurisdictionAliases.has(j) ? index.jurisdictionAliases.get(j)! : j;

  for (const g of groups) {
    if (g.all) {
      const j = mapJ(g.jurisdiction);
      take(j === null ? null : `calendar/${j}/all.ics`);
      continue;
    }
    for (const b of g.bodies) {
      const aliasKey = `${g.jurisdiction}/${b}`;
      let target: string | null;
      if (index.bodyAliases.has(aliasKey)) {
        target = index.bodyAliases.get(aliasKey)!;
      } else {
        const j = mapJ(g.jurisdiction);
        target = j === null ? null : `${j}/${b}`;
      }
      take(target === null ? null : `calendar/${target}.ics`);
    }
  }

  // A body feed is redundant when its jurisdiction's combined feed is also selected.
  const combined = new Set(picked.filter((e) => e.kind === 'combined').map((e) => e.jurisdiction));
  const entries = picked.filter((e) => e.kind === 'combined' || !combined.has(e.jurisdiction));
  return { entries, missing };
}
