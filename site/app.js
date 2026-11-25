// Provenage feed builder. Plain ES module, no build step, no third-party code.
//
// Safety rules (enforced by tests in site/test):
//  * Every string that came from the index reaches the page through textContent or setAttribute on elements
//    made with createElement. No HTML-parsing APIs are used anywhere.
//  * Links are built only from slugs that pass SLUG_RE, never from the index "href" field.
import { MAX_REFS, isSlug, parseSelection, buildSelection } from './canon.js';

const DEFAULT_BASE = 'https://api.1507.systems/provenage';
const INDEX_SCHEMA = 'provenage-calendar-index/1';
const FETCH_TIMEOUT_MS = 15000;
const CALENDAR_ORIGIN_PATH = '/calendar/';

const $ = (id) => document.getElementById(id);

/**
 * The API base. The shipped page has no override; tests may add <meta name="provenage-base" content="..."> to a
 * copy of the page (with a matching CSP) to point at a fixture server.
 */
function getBase() {
  const meta = document.querySelector('meta[name="provenage-base"]');
  const v = meta ? meta.getAttribute('content') : null;
  if (v && /^https?:\/\/[^?#\s]+$/.test(v)) return v.replace(/\/+$/, '');
  return DEFAULT_BASE;
}
const BASE = getBase();

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v === false || v === null || v === undefined) continue;
    node.setAttribute(k, v === true ? '' : String(v));
  }
  for (const c of children) node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return node;
}

/** Case- and diacritic-insensitive form used by the filter. */
const fold = (s) => String(s).normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase();
const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
const clip = (s, n = 80) => (s.length > n ? `${s.slice(0, n)}…` : s);

// ---------- state ----------
/** @type {{max:number, maxBytes:number, jurisdictions:Map<string,object>, aliasB:Map, aliasJ:Map}|null} */
let model = null;
/** jurisdiction slug -> {all:boolean, bodies:Set<string>} */
const sel = new Map();
/** checkbox elements: key "j" for All, "j/b" for a body */
const boxes = new Map();
const groupEls = new Map();

// ---------- index loading ----------
const isObj = (v) => typeof v === 'object' && v !== null && !Array.isArray(v);

function aliasMap(raw) {
  const m = new Map();
  if (!isObj(raw)) return m;
  for (const [k, v] of Object.entries(raw)) if (v === null || typeof v === 'string') m.set(k, v);
  return m;
}

/** Validate the index and build the model. Returns {error} when the page cannot use it. */
function buildModel(doc) {
  if (!isObj(doc) || doc.schema !== INDEX_SCHEMA || !isObj(doc.mix) || typeof doc.mix.version !== 'number') {
    return { error: 'schema' };
  }
  if (doc.mix.version !== 1) return { error: 'schema' };
  const num = (v, d) => (Number.isFinite(v) && v > 0 ? v : d);
  const m = {
    max: Math.min(num(doc.mix.max_refs, MAX_REFS), MAX_REFS),
    maxBytes: num(doc.mix.issue_max_bytes, 2000000),
    jurisdictions: new Map(),
    aliasB: aliasMap(isObj(doc.aliases) ? doc.aliases.bodies : null),
    aliasJ: aliasMap(isObj(doc.aliases) ? doc.aliases.jurisdictions : null),
  };
  const meta = isObj(doc.jurisdictions) ? doc.jurisdictions : {};
  const ensure = (slug) => {
    let j = m.jurisdictions.get(slug);
    if (!j) {
      const info = isObj(meta[slug]) ? meta[slug] : {};
      const name = typeof info.name === 'string' && info.name ? info.name : slug;
      j = { slug, name, dev: info.status === 'dev', all: null, bodies: new Map() };
      m.jurisdictions.set(slug, j);
    }
    return j;
  };
  const feeds = Array.isArray(doc.feeds) ? doc.feeds : [];
  for (const f of feeds) {
    if (!isObj(f) || !isSlug(f.jurisdiction) || !isSlug(f.body)) continue;
    const size = Number.isFinite(f.size) && f.size >= 0 ? f.size : 0;
    const events = Number.isFinite(f.events) && f.events >= 0 ? f.events : 0;
    const j = ensure(f.jurisdiction);
    if (f.body === 'all') {
      j.all = { size, events };
    } else {
      const name = typeof f.body_name === 'string' && f.body_name ? f.body_name : f.body;
      j.bodies.set(f.body, { slug: f.body, name, size, events });
    }
  }
  for (const [slug, j] of [...m.jurisdictions]) if (!j.all && j.bodies.size === 0) m.jurisdictions.delete(slug);
  return { model: m };
}

async function fetchIndex() {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), FETCH_TIMEOUT_MS);
  try {
    const res = await fetch(`${BASE}${CALENDAR_ORIGIN_PATH}index.json`, {
      signal: ctl.signal,
      credentials: 'omit',
      referrerPolicy: 'no-referrer',
      cache: 'no-cache',
    });
    if (!res.ok) throw new Error('bad status');
    return await res.json();
  } finally {
    clearTimeout(timer);
  }
}

// ---------- selection helpers ----------
function refCount() {
  let n = 0;
  for (const v of sel.values()) n += v.all ? 1 : v.bodies.size;
  return n;
}

function selectedBytes() {
  let total = 0;
  for (const [slug, v] of sel) {
    const j = model.jurisdictions.get(slug);
    if (!j) continue;
    if (v.all) total += j.all ? j.all.size : 0;
    else for (const b of v.bodies) total += j.bodies.get(b)?.size ?? 0;
  }
  return total;
}

function setAll(slug, on) {
  if (on) sel.set(slug, { all: true, bodies: new Set() });
  else sel.delete(slug);
}

function setBody(slug, body, on) {
  let v = sel.get(slug);
  if (!v) {
    v = { all: false, bodies: new Set() };
    sel.set(slug, v);
  }
  if (on) v.bodies.add(body);
  else v.bodies.delete(body);
  if (!v.all && v.bodies.size === 0) sel.delete(slug);
}

// ---------- rendering the list ----------
function renderGroups() {
  const root = $('groups');
  root.replaceChildren();
  boxes.clear();
  groupEls.clear();
  const list = [...model.jurisdictions.values()].sort((a, b) => a.name.localeCompare(b.name));
  for (const j of list) {
    const bodies = [...j.bodies.values()].sort((a, b) => a.name.localeCompare(b.name));
    const summary = el('summary', {}, el('span', {}, j.name), ` (${plural(bodies.length, 'board', 'boards')})`);
    if (j.dev) summary.append(el('span', { class: 'badge' }, 'Test data'));
    const selCount = el('span', { class: 'badge', hidden: true });
    summary.append(selCount);

    const inner = el('div', { class: 'inner' });
    const ul = el('ul');
    const items = [];

    if (j.all) {
      const cb = el('input', { type: 'checkbox' });
      cb.addEventListener('change', () => {
        setAll(j.slug, cb.checked);
        sync();
      });
      boxes.set(j.slug, cb);
      const li = el('li', {}, el('label', { class: 'opt all' }, cb, el('span', {}, `All meetings in ${j.name} (includes boards added later)`)));
      ul.append(li);
    }
    const included = el('p', { class: 'muted', hidden: true }, 'Included in All meetings');
    ul.append(el('li', {}, included));

    for (const b of bodies) {
      const cb = el('input', { type: 'checkbox' });
      cb.addEventListener('change', () => {
        setBody(j.slug, b.slug, cb.checked);
        sync();
      });
      boxes.set(`${j.slug}/${b.slug}`, cb);
      const li = el('li', {}, el('label', { class: 'opt' }, cb, el('span', {}, `${b.name} (${plural(b.events, 'meeting', 'meetings')})`)));
      items.push({ li, text: fold(b.name) });
      ul.append(li);
    }
    inner.append(ul);
    const det = el('details', { class: 'group' }, summary, inner);
    if (list.length <= 3) det.setAttribute('open', '');
    root.append(det);
    groupEls.set(j.slug, { det, included, selCount, items, jText: fold(`${j.name} ${j.slug}`) });
  }
  applyFilter();
}

function applyFilter() {
  const q = fold($('filter').value.trim());
  let shownCals = 0;
  let totalCals = 0;
  for (const [slug, g] of groupEls) {
    const j = model.jurisdictions.get(slug);
    totalCals += j.bodies.size + (j.all ? 1 : 0);
    const placeMatch = q === '' || g.jText.includes(q);
    let any = placeMatch;
    let shown = placeMatch ? j.bodies.size + (j.all ? 1 : 0) : 0;
    for (const it of g.items) {
      const m = placeMatch || it.text.includes(q);
      it.li.hidden = !m;
      if (m && !placeMatch) {
        any = true;
        shown += 1;
      }
    }
    g.det.hidden = !any;
    if (q !== '' && any) g.det.setAttribute('open', '');
    shownCals += any ? shown : 0;
  }
  $('count').textContent = q === '' ? '' : `Showing ${shownCals} of ${totalCals} calendars.`;
}

// ---------- sync UI with the selection ----------
function sync() {
  const refs = refCount();
  const atCap = refs >= model.max;
  const over = selectedBytes() > model.maxBytes;

  for (const [slug, j] of model.jurisdictions) {
    const g = groupEls.get(slug);
    const v = sel.get(slug);
    const allOn = !!v?.all;
    const allBox = boxes.get(slug);
    if (allBox) {
      allBox.checked = allOn;
      allBox.disabled = !allOn && atCap && !(v && v.bodies.size > 0);
    }
    g.included.hidden = !allOn;
    for (const b of j.bodies.keys()) {
      const cb = boxes.get(`${slug}/${b}`);
      const on = !allOn && !!v?.bodies.has(b);
      cb.checked = on;
      cb.disabled = allOn || (!on && atCap);
    }
    const n = v ? (v.all ? 1 : v.bodies.size) : 0;
    g.selCount.hidden = n === 0;
    g.selCount.textContent = n === 0 ? '' : `${n} selected`;
  }

  const capNote = $('cap-note');
  capNote.hidden = !atCap;
  capNote.textContent = atCap ? `Up to ${model.max} calendars in one address. Make a second one for more.` : '';

  renderResult(refs, over);
  mirrorToFragment();
}

function currentSelection() {
  return buildSelection(sel);
}

function addressFor(selection) {
  return `${BASE}/mix/v1/${selection}.ics`;
}

const webcalOf = (https) => https.replace(/^https?:\/\//, 'webcal://');

function renderResult(refs, over) {
  const selection = currentSelection();
  const parsed = selection ? parseSelection(selection) : null;
  const issued = !!parsed && parsed.ok && !over;
  const address = issued ? addressFor(selection) : '';

  $('address').value = address;
  $('result-hint').textContent = issued
    ? `${plural(refs, 'calendar', 'calendars')} in one address.`
    : 'Choose at least one calendar above to get an address.';

  const https = address;
  const webcal = issued ? webcalOf(address) : '';
  const targets = {
    copy: issued,
    apple: issued,
    google: issued,
    outlook: issued,
    m365: issued,
  };
  for (const [id, on] of Object.entries(targets)) $(id).disabled = !on;
  actions.apple = webcal;
  actions.google = issued ? `https://calendar.google.com/calendar/render?cid=${encodeURIComponent(webcal)}` : '';
  actions.outlook = issued ? `https://outlook.live.com/calendar/0/addfromweb?url=${encodeURIComponent(https)}` : '';
  actions.m365 = issued ? `https://outlook.office.com/calendar/0/addfromweb?url=${encodeURIComponent(https)}` : '';
  actions.copy = address;

  const notes = $('notes');
  notes.replaceChildren();
  if (over) {
    notes.append(el('p', { class: 'note' }, 'These calendars add up to more than one address can carry, so no address is issued. Untick a few, and make a second address for the rest.'));
  }
  let devSelected = false;
  for (const [slug, v] of sel) {
    if (model.jurisdictions.get(slug)?.dev && (v.all || v.bodies.size)) devSelected = true;
  }
  if (devSelected) notes.append(el('p', { class: 'note' }, 'This address is for testing and may stop working.'));

  renderSeparate();
}

function renderSeparate() {
  const list = $('separate-list');
  list.replaceChildren();
  let n = 0;
  for (const [slug, v] of [...sel].sort((a, b) => (a[0] < b[0] ? -1 : 1))) {
    const j = model.jurisdictions.get(slug);
    if (!j) continue;
    const add = (label, file) => {
      // Built only from validated slugs, never from the index href field.
      const href = `${BASE}${CALENDAR_ORIGIN_PATH}${encodeURIComponent(slug)}/${encodeURIComponent(file)}.ics`;
      list.append(el('li', {}, el('a', { href, rel: 'noopener noreferrer' }, label)));
      n += 1;
    };
    if (v.all) add(`${j.name}: All meetings`, 'all');
    else for (const b of [...v.bodies].sort()) add(`${j.name}: ${j.bodies.get(b)?.name ?? b}`, b);
  }
  $('separate').hidden = n === 0;
}

// ---------- actions ----------
const actions = { copy: '', apple: '', google: '', outlook: '', m365: '' };

function say(text) {
  $('action-msg').textContent = text;
}

async function doCopy() {
  const input = $('address');
  const text = actions.copy;
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
    say('Copied. Paste it into your calendar app as a subscription by address.');
  } catch {
    input.focus();
    input.select();
    say('Press Ctrl+C (or Cmd+C) to copy the selected address.');
  }
}

function wireActions() {
  $('copy').addEventListener('click', doCopy);
  $('apple').addEventListener('click', () => {
    if (actions.apple) window.location.href = actions.apple;
  });
  for (const id of ['google', 'outlook', 'm365']) {
    $(id).addEventListener('click', () => {
      if (!actions[id]) return;
      window.open(actions[id], '_blank', 'noopener,noreferrer');
      if (id === 'google') say('Google Calendar: use a computer; the Google phone app cannot add by address.');
    });
  }
  $('address').addEventListener('focus', () => $('address').select());
}

// ---------- page address (fragment) ----------
function mirrorToFragment() {
  const selection = currentSelection();
  const want = selection ? `#v1=${encodeURIComponent(selection)}` : '';
  if (location.hash === want || (want === '' && location.hash === '')) return;
  try {
    history.replaceState(null, '', `${location.pathname}${location.search}${want}`);
  } catch {
    /* some embedders forbid it; the page still works */
  }
}

const FRAG_RE = /^#v1=([a-z0-9._-]*)$/;

/**
 * Tick boxes from selection text (a fragment or a pasted address). Lenient: parts that cannot be used are
 * reported, never silently dropped. Renamed parts follow the index aliases.
 */
function applySelectionText(text) {
  sel.clear();
  const problems = [];
  let renamed = 0;
  const parts = text.split('_');
  for (const part of parts) {
    const seg = part.split('.');
    if (seg.length < 2) {
      if (part) problems.push(clip(part));
      continue;
    }
    const [jRaw, ...members] = seg;
    for (const m of members) {
      const label = clip(`${jRaw}.${m}`);
      if (!isSlug(jRaw) || !isSlug(m)) {
        problems.push(clip(`${jRaw}.${m}`));
        continue;
      }
      let jSlug = jRaw;
      let body = m;
      if (m !== 'all' && model.aliasB.has(`${jRaw}/${m}`)) {
        const t = model.aliasB.get(`${jRaw}/${m}`);
        if (t === null) {
          problems.push(`${label} (no longer published)`);
          continue;
        }
        [jSlug, body] = t.split('/');
        renamed += 1;
      } else if (model.aliasJ.has(jRaw)) {
        const t = model.aliasJ.get(jRaw);
        if (t === null) {
          problems.push(`${label} (no longer published)`);
          continue;
        }
        jSlug = t;
        renamed += 1;
      }
      const j = model.jurisdictions.get(jSlug);
      if (!j || !isSlug(body) || (body === 'all' ? !j.all : !j.bodies.has(body))) {
        problems.push(`${label} (not found)`);
        continue;
      }
      if (refCount() >= model.max && !sel.get(jSlug)) {
        problems.push(`${label} (over the ${model.max} calendar limit)`);
        continue;
      }
      if (body === 'all') setAll(jSlug, true);
      else if (!sel.get(jSlug)?.all) setBody(jSlug, body, true);
    }
  }
  const box = $('restore');
  box.replaceChildren();
  if (problems.length || renamed) {
    if (renamed) box.append(el('p', {}, `${plural(renamed, 'part was', 'parts were')} renamed since that address was made; the current names are ticked.`));
    if (problems.length) {
      box.append(el('p', {}, 'These parts of that address could not be ticked:'));
      const ul = el('ul', { class: 'plain' });
      for (const p of problems.slice(0, 20)) ul.append(el('li', {}, p));
      box.append(ul);
    }
  }
  box.hidden = box.childElementCount === 0;
  sync();
}

function restoreFromFragment() {
  const m = FRAG_RE.exec(location.hash);
  if (m && m[1]) applySelectionText(m[1]);
}

function pasteAddress() {
  const raw = $('paste').value.trim();
  const m = /\/mix\/v1\/([a-z0-9._-]+)\.ics(?:[?#].*)?$/.exec(raw);
  if (!m) {
    const box = $('restore');
    box.replaceChildren(el('p', {}, 'That does not look like an address made by this page. It should contain /mix/v1/ and end in .ics.'));
    box.hidden = false;
    return;
  }
  applySelectionText(m[1]);
}

// ---------- boot ----------
function showState(which, text) {
  $('state').hidden = which !== 'loading';
  $('error').hidden = which !== 'error';
  $('picker').hidden = which !== 'ready';
  $('result').hidden = which !== 'ready';
  if (which === 'error') {
    $('error-text').textContent = text.message;
    $('retry').hidden = !!text.reload;
    $('reload').hidden = !text.reload;
  }
}

async function load() {
  showState('loading');
  let doc;
  try {
    doc = await fetchIndex();
  } catch {
    showState('error', { message: 'Could not load the list of calendars. Check your connection and try again.' });
    return;
  }
  const built = buildModel(doc);
  if (built.error) {
    showState('error', { message: 'This page is out of date. Reload it to continue.', reload: true });
    return;
  }
  model = built.model;
  showState('ready');
  renderGroups();
  restoreFromFragment();
  sync();
}

$('retry').addEventListener('click', load);
$('reload').addEventListener('click', () => location.reload());
$('filter').addEventListener('input', () => model && applyFilter());
$('paste-go').addEventListener('click', () => model && pasteAddress());
window.addEventListener('hashchange', () => {
  if (!model) return;
  if (FRAG_RE.test(location.hash)) restoreFromFragment();
  else {
    sel.clear();
    sync();
  }
});
wireActions();
load();
