/**
 * Pure grammar and canonical-form functions for /mix/v1/ selections (no DOM, no network).
 * A line-for-line port of workers/feeds/src/mix/parse.ts; both are tested against the shared vectors in
 * workers/feeds/test/mix-vectors.json. The grammar is frozen as v1 (see docs/CALENDAR.md).
 *
 *   selection = group *( "_" group )
 *   group     = slug "." member
 *   member    = "all" / body *( "." body )
 */

export const MAX_REFS = 32;
export const MAX_SELECTION_BYTES = 1536;
export const MAX_SLUG = 64;
export const SLUG_RE = /^[a-z0-9]+(-[a-z0-9]+)*$/; // = validate.SLUG_RE; linear and anchored

export const isSlug = (s) => typeof s === 'string' && s.length >= 1 && s.length <= MAX_SLUG && SLUG_RE.test(s);

const refCount = (groups) => groups.reduce((n, g) => n + (g.all ? 1 : g.bodies.length), 0);

/** Serialise groups (already in canonical order) back to a selection string. */
export function selectionString(groups) {
  return groups.map((g) => `${g.jurisdiction}.${g.all ? 'all' : g.bodies.join('.')}`).join('_');
}

/** Sort and merge repeats. Returns null when a repeat cannot be merged (same place as both "all" and bodies). */
function canonicalize(groups) {
  const by = new Map();
  for (const g of groups) {
    const prev = by.get(g.jurisdiction);
    if (!prev) {
      by.set(g.jurisdiction, { jurisdiction: g.jurisdiction, all: g.all, bodies: [...g.bodies] });
    } else if (prev.all !== g.all) {
      return null;
    } else {
      prev.bodies.push(...g.bodies);
    }
  }
  const out = [...by.values()].sort((a, b) => (a.jurisdiction < b.jurisdiction ? -1 : 1));
  for (const g of out) g.bodies = [...new Set(g.bodies)].sort();
  if (refCount(out) > MAX_REFS) return null;
  return out;
}

/**
 * Parse a selection (the text between "/mix/v1/" and ".ics").
 * Returns {ok:true, groups, refs, selection} or {ok:false, error, canonical?} where canonical is the fixed
 * selection string (not a path) when the only problem is order or repeats.
 */
export function parseSelection(selection) {
  if (typeof selection !== 'string') return { ok: false, error: 'not a valid selection' };
  // Length first, in bytes. Non-ASCII is rejected below, but an oversized string is never parsed.
  if (selection.length > MAX_SELECTION_BYTES || new TextEncoder().encode(selection).length > MAX_SELECTION_BYTES) {
    return { ok: false, error: 'selection is too long' };
  }
  if (selection.length === 0) return { ok: false, error: 'empty selection' };

  const parsed = [];
  for (const part of selection.split('_')) {
    const seg = part.split('.');
    if (seg.length < 2) return { ok: false, error: 'each group must be <jurisdiction>.<bodies or all>' };
    const [jurisdiction, ...members] = seg;
    if (!isSlug(jurisdiction)) return { ok: false, error: 'invalid jurisdiction name' };
    for (const m of members) if (!isSlug(m)) return { ok: false, error: 'invalid body name' };
    const hasAll = members.includes('all');
    if (hasAll && members.length > 1) return { ok: false, error: '"all" must be the only member of its group' };
    parsed.push({ jurisdiction, all: hasAll, bodies: hasAll ? [] : members });
  }

  if (refCount(parsed) > MAX_REFS) return { ok: false, error: `too many calendars (at most ${MAX_REFS})` };

  const fixed = canonicalize(parsed);
  if (fixed && selectionString(fixed) === selection) {
    return { ok: true, groups: fixed, refs: refCount(fixed), selection };
  }
  if (fixed) return { ok: false, error: 'selection is not in canonical form', canonical: selectionString(fixed) };
  return { ok: false, error: 'a jurisdiction may appear only once' };
}

/**
 * Build the canonical selection string from a Map of jurisdiction slug -> {all:boolean, bodies:Set<string>}.
 * Empty groups are skipped. Returns '' when nothing is selected.
 */
export function buildSelection(sel) {
  const groups = [];
  for (const [jurisdiction, v] of sel) {
    const bodies = [...v.bodies].sort();
    if (!v.all && bodies.length === 0) continue;
    groups.push({ jurisdiction, all: v.all, bodies: v.all ? [] : bodies });
  }
  groups.sort((a, b) => (a.jurisdiction < b.jurisdiction ? -1 : 1));
  return selectionString(groups);
}
