// Runs the shared Worker grammar vectors against site/canon.js (node --test).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { parseSelection, buildSelection, MAX_REFS, MAX_SELECTION_BYTES } from '../../canon.js';

const vectors = JSON.parse(readFileSync(new URL('../../../workers/feeds/test/mix-vectors.json', import.meta.url), 'utf8')).vectors;

test('vectors file is present and non-trivial', () => {
  assert.ok(vectors.length >= 30);
});

for (const v of vectors) {
  test(`vector: ${v.name}`, () => {
    const r = parseSelection(v.selection);
    assert.equal(r.ok, v.valid);
    if (v.valid) {
      assert.equal(r.selection, v.canonical_selection);
    } else {
      assert.equal(r.canonical ?? null, v.canonical_selection ?? null);
      if (v.error_contains) assert.ok(r.error.includes(v.error_contains), `${r.error} should contain ${v.error_contains}`);
    }
  });
}

test('limits match the Worker', () => {
  assert.equal(MAX_REFS, 32);
  assert.equal(MAX_SELECTION_BYTES, 1536);
});

test('a selection over 1,536 bytes is refused before parsing', () => {
  const s = `${'a'.repeat(60)}.all`.repeat(30).split('.all').filter(Boolean).map((x, i) => `${x}${i}.all`).join('_');
  assert.ok(s.length > MAX_SELECTION_BYTES);
  const r = parseSelection(s);
  assert.equal(r.ok, false);
  assert.match(r.error, /too long/);
});

test('buildSelection output always parses as canonical', () => {
  const sel = new Map([
    ['zeta', { all: false, bodies: new Set(['b', 'a']) }],
    ['alpha', { all: true, bodies: new Set() }],
    ['empty', { all: false, bodies: new Set() }],
  ]);
  const s = buildSelection(sel);
  assert.equal(s, 'alpha.all_zeta.a.b');
  assert.equal(parseSelection(s).ok, true);
  assert.equal(buildSelection(new Map()), '');
});
