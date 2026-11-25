/**
 * /mix/v1/<selection>.ics grammar (frozen as v1; see docs/CALENDAR.md).
 *
 *   path      = "/mix/v1/" selection ".ics"
 *   selection = group *( "_" group )
 *   group     = slug "." member
 *   member    = "all" / body *( "." body )
 *
 * The parser is split-based (no backtracking regex) and runs the length check first. Nothing here touches
 * storage. Error strings are fixed text and never echo request input.
 */

export const MAX_REFS = 32;
export const MAX_SELECTION_BYTES = 1536;
/** The public address prefix the router adds in front of this Worker's paths. */
export const PUBLIC_PREFIX = '/provenage';

const PREFIX = '/mix/v1/';
const SUFFIX = '.ics';
const SLUG_RE = /^[a-z0-9]+(-[a-z0-9]+)*$/; // = validate.SLUG_RE; linear, anchored
const MAX_SLUG = 64;

export interface Group {
  jurisdiction: string;
  /** 'all' or a list of body slugs (canonical order once parsed from a canonical selection). */
  all: boolean;
  bodies: string[];
}

export type ParseResult =
  | { ok: true; groups: Group[]; refs: number; selection: string }
  | { ok: false; error: string; canonical?: string };

const bad = (error: string, canonical?: string): ParseResult =>
  canonical === undefined ? { ok: false, error } : { ok: false, error, canonical };

const validSlug = (s: string) => s.length >= 1 && s.length <= MAX_SLUG && SLUG_RE.test(s);

function refCount(groups: Group[]): number {
  return groups.reduce((n, g) => n + (g.all ? 1 : g.bodies.length), 0);
}

/** Serialise groups (already in canonical order) back to a selection string. */
export function selectionString(groups: Group[]): string {
  return groups.map((g) => `${g.jurisdiction}.${g.all ? 'all' : g.bodies.join('.')}`).join('_');
}

/** `pathname` is the Worker-side path (router prefix already stripped). */
export function parseMixPath(pathname: string): ParseResult {
  if (!pathname.startsWith(PREFIX) || !pathname.endsWith(SUFFIX) || pathname.length <= PREFIX.length + SUFFIX.length) {
    return bad('not a valid selection address');
  }
  const selection = pathname.slice(PREFIX.length, pathname.length - SUFFIX.length);
  return parseSelection(selection);
}

export function parseSelection(selection: string): ParseResult {
  // Length first, in bytes (non-ASCII is rejected below, but never parse an oversized string).
  if (selection.length > MAX_SELECTION_BYTES || new TextEncoder().encode(selection).length > MAX_SELECTION_BYTES) {
    return bad('selection is too long');
  }
  if (selection.length === 0) return bad('empty selection');

  const parsed: Group[] = [];
  for (const part of selection.split('_')) {
    const seg = part.split('.');
    if (seg.length < 2) return bad('each group must be <jurisdiction>.<bodies or all>');
    const [jurisdiction, ...members] = seg;
    if (!validSlug(jurisdiction)) return bad('invalid jurisdiction name');
    for (const m of members) if (!validSlug(m)) return bad('invalid body name');
    const hasAll = members.includes('all');
    if (hasAll && members.length > 1) return bad('"all" must be the only member of its group');
    parsed.push({ jurisdiction, all: hasAll, bodies: hasAll ? [] : members });
  }

  const literalRefs = refCount(parsed);
  if (literalRefs > MAX_REFS) return bad(`too many calendars (at most ${MAX_REFS})`);

  // Canonical form, checked on the literal string.
  const fixed = canonicalize(parsed);
  if (fixed && selectionString(fixed) === selection) {
    return { ok: true, groups: fixed, refs: refCount(fixed), selection };
  }
  if (fixed) {
    return bad('selection is not in canonical form', `${PUBLIC_PREFIX}${PREFIX}${selectionString(fixed)}${SUFFIX}`);
  }
  return bad('a jurisdiction may appear only once');
}

/**
 * Sort groups and bodies, merge repeats. Returns null when a repeat cannot be merged without changing meaning
 * (the same jurisdiction given as both "all" and bodies).
 */
function canonicalize(groups: Group[]): Group[] | null {
  const by = new Map<string, Group>();
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
