/**
 * Merge of already-built feeds into one calendar (resolved spec section 5).
 *
 * Event spans are copied as byte ranges, unchanged, except that (when the selection spans more than one
 * jurisdiction) the first line of each SUMMARY gets a place prefix inserted with a fold break after it, so no
 * existing line is re-split. Every string that came from the index is control-stripped and TEXT-escaped before
 * it reaches a content line (header-injection defence).
 */

import { fold, foldJoin } from './fold';

export const PAGE_URL = 'https://1507-systems.github.io/provenage/';
export const MAX_OUTPUT_BYTES = 8 * 1024 * 1024;
const NAME_MAX_CHARS = 120;
const DESC_MAX_OCTETS = 1000;
const SHORT_LIST_MAX = 60;

const HEAD = 'BEGIN:VCALENDAR\r\n';
const TAIL = 'END:VCALENDAR\r\n';
const EV_BEGIN = 'BEGIN:VEVENT\r\n';
const EV_END = 'END:VEVENT\r\n';
const TZ_BEGIN = 'BEGIN:VTIMEZONE\r\n';
const TZ_END = 'END:VTIMEZONE\r\n';

export class SourceShapeError extends Error {}

export interface ParsedSource {
  /** [TZID, block text incl. trailing CRLF] in source order. */
  zones: Array<[string, string]>;
  /** Event span: first BEGIN:VEVENT line up to (not including) END:VCALENDAR; '' when no events. */
  events: string;
}

/** Parse a built feed's text; throws SourceShapeError when it is not laid out the way the builder writes it. */
export function parseSource(text: string): ParsedSource {
  if (!text.startsWith(HEAD) || !text.endsWith(TAIL) || text.length < HEAD.length + TAIL.length) {
    throw new SourceShapeError('envelope');
  }
  const body = text.slice(0, text.length - TAIL.length);
  const firstEv = body.indexOf('\r\n' + EV_BEGIN);
  const headerEnd = firstEv === -1 ? body.length : firstEv + 2;
  const events = firstEv === -1 ? '' : body.slice(firstEv + 2);
  if (events && (!events.endsWith(EV_END) || events.includes('\r\n' + TZ_BEGIN))) {
    throw new SourceShapeError('events');
  }
  const zones: Array<[string, string]> = [];
  let pos = 0;
  const header = body.slice(0, headerEnd);
  for (;;) {
    const at = header.indexOf('\r\n' + TZ_BEGIN, pos);
    if (at === -1) break;
    const start = at + 2;
    const endMark = header.indexOf('\r\n' + TZ_END, start);
    if (endMark === -1) throw new SourceShapeError('timezone');
    const block = header.slice(start, endMark + 2 + TZ_END.length);
    const tz = /\r\nTZID:([^\r\n]+)\r\n/.exec('\r\n' + block);
    if (!tz) throw new SourceShapeError('timezone id');
    zones.push([tz[1], block]);
    pos = endMark + 2 + TZ_END.length;
  }
  return { zones, events };
}

/** Remove U+0000-U+001F and U+007F. */
export function stripControls(s: string): string {
  return s.replace(/[\u0000-\u001f\u007f]/g, '');
}

/** RFC 5545 TEXT escaping for a single-line value (controls are stripped first, so no newline form is needed). */
export function escapeText(s: string): string {
  return s.replace(/\\/g, '\\\\').replace(/;/g, '\\;').replace(/,/g, '\\,');
}

function truncateChars(s: string, n: number): string {
  const cps = Array.from(s);
  return cps.length <= n ? s : cps.slice(0, n).join('');
}

function truncateOctets(s: string, max: number): string {
  const enc = new TextEncoder();
  let out = '';
  let used = 0;
  for (const ch of s) {
    const n = enc.encode(ch).length;
    if (used + n > max) break;
    out += ch;
    used += n;
  }
  return out;
}

/** Clean an index-derived string for a content value: strip, cap on a character boundary, then escape. */
export function textValue(s: string, maxChars: number): string {
  return escapeText(truncateChars(stripControls(s), maxChars));
}

export interface HeaderInput {
  /** Resolved entries' display data, in resolved order. */
  items: Array<{ jurisdiction: string; shortName: string; title: string; bodyName: string | null; combined: boolean }>;
  canonicalSelection: string;
  missing: number;
}

/** The calendar name, before cleaning (section 5.2). */
export function calendarName(items: HeaderInput['items']): string {
  if (items.length === 1) return items[0].title;
  const places: string[] = [];
  for (const it of items) if (!places.includes(it.shortName)) places.push(it.shortName);
  const labels = items.map((it) => (it.combined ? 'All meetings' : (it.bodyName ?? it.title)));
  if (places.length === 1) {
    const full = `${places[0]}: ${labels.join(', ')}`;
    if (Array.from(stripControls(full)).length <= SHORT_LIST_MAX) return full;
    return `${places[0]}: ${labels[0]} + ${labels.length - 1} more`;
  }
  const full = `Local meetings: ${places.join(', ')}`;
  if (Array.from(stripControls(full)).length <= SHORT_LIST_MAX) return full;
  return `Local meetings: ${places.length} places`;
}

export function calendarDescription(h: HeaderInput): string {
  let d = `Unofficial merged calendar from Provenage. Change this selection: ${PAGE_URL}#v1=${h.canonicalSelection}`;
  if (h.missing > 0) d += ` ${h.missing} selected calendars are no longer published.`;
  return d;
}

/** The index-derived part of the output header: X-WR-CALNAME and X-WR-CALDESC lines (folded, CRLF-joined). */
export function nameDescLines(h: HeaderInput): string {
  const name = textValue(calendarName(h.items), NAME_MAX_CHARS);
  const desc = escapeText(truncateOctets(stripControls(calendarDescription(h)), DESC_MAX_OCTETS));
  return `${foldJoin('X-WR-CALNAME:' + name)}\r\n${foldJoin('X-WR-CALDESC:' + desc)}\r\n`;
}

/** Insert "<short>: " + fold break after "SUMMARY:" on every SUMMARY first line. */
export function prefixSummaries(events: string, shortName: string): string {
  const prefixLine = fold('SUMMARY:' + textValue(shortName, 40) + ': ');
  // fold() pieces after the first start with a space already; the final piece is followed by a fold break.
  const replacement = prefixLine.join('\r\n') + '\r\n ';
  return events.replace(/\r\nSUMMARY:/g, () => '\r\n' + replacement);
}

export function mergeSources(
  header: HeaderInput,
  sources: Array<{ parsed: ParsedSource; shortName: string }>,
  prefix: boolean,
): string {
  const zones = new Map<string, string>();
  for (const { parsed } of sources) {
    for (const [tzid, block] of parsed.zones) {
      const have = zones.get(tzid);
      if (have === undefined || block.length > have.length) zones.set(tzid, block);
    }
  }
  const tzids = [...zones.keys()].sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));

  let out = HEAD + 'VERSION:2.0\r\nPRODID:-//Provenage//mix v1//EN\r\nCALSCALE:GREGORIAN\r\nMETHOD:PUBLISH\r\n';
  out += nameDescLines(header);
  if (tzids.length === 1) out += foldJoin('X-WR-TIMEZONE:' + tzids[0]) + '\r\n';
  out += 'REFRESH-INTERVAL;VALUE=DURATION:PT1H\r\nX-PUBLISHED-TTL:PT1H\r\n';
  for (const t of tzids) out += zones.get(t)!;
  for (const s of sources) out += prefix ? prefixSummaries(s.parsed.events, s.shortName) : s.parsed.events;
  return out + TAIL;
}
