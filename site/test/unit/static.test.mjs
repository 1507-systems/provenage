// Static checks on the page source: banned HTML-injection APIs, strict CSP, no inline code, no third parties.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, existsSync, readdirSync } from 'node:fs';

const read = (p) => readFileSync(new URL(`../../${p}`, import.meta.url), 'utf8');
const html = read('index.html');
const app = read('app.js');
const css = read('style.css');

test('banned HTML-injection APIs are absent', () => {
  const banned = /innerHTML|outerHTML|insertAdjacentHTML|document\.write|createContextualFragment|DOMParser|\beval\s*\(|new Function|setTimeout\s*\(\s*['"`]|srcdoc/;
  for (const [name, src] of [['app.js', app], ['canon.js', read('canon.js')]]) assert.doesNotMatch(src, banned, name);
});

test('CSP meta is exactly the specified policy', () => {
  const m = /<meta http-equiv="Content-Security-Policy" content="([^"]+)"/.exec(html);
  assert.ok(m, 'CSP meta present');
  assert.equal(
    m[1],
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src https://api.1507.systems; img-src 'self' data:; base-uri 'none'; form-action 'none'",
  );
});

test('no inline scripts, inline styles or event-handler attributes', () => {
  assert.doesNotMatch(html, /<script(?![^>]*\bsrc=)[^>]*>/i);
  assert.doesNotMatch(html, /<style\b/i);
  assert.doesNotMatch(html, /\sstyle\s*=/i);
  assert.doesNotMatch(html, /\son[a-z]+\s*=/i);
  assert.doesNotMatch(app, /setAttribute\(\s*['"](style|on[a-z]+)['"]/i);
});

test('no third-party code: only same-origin script and stylesheet', () => {
  for (const m of html.matchAll(/<(?:script|link)\b[^>]*\b(?:src|href)="([^"]+)"/gi)) {
    assert.doesNotMatch(m[1], /^(https?:)?\/\//, `external resource ${m[1]}`);
  }
  assert.doesNotMatch(css, /@import|url\(\s*['"]?https?:/);
  assert.doesNotMatch(app, /\bimport\s*\(|from\s+['"]https?:/);
});

test('app.js names only the expected hosts', () => {
  const hosts = [...new Set([...app.matchAll(/https?:\/\/[a-z0-9.-]+/g)].map((m) => m[0]))].sort();
  assert.deepEqual(hosts, ['https://api.1507.systems', 'https://calendar.google.com', 'https://outlook.live.com', 'https://outlook.office.com']);
});

test('index href field is never read', () => {
  assert.doesNotMatch(app, /\b(?:f|feed|entry|item)\.href\b|\[['"]href['"]\]/);
});

test('page files exist and the Pages workflow uploads only the page files', () => {
  const files = ['index.html', 'app.js', 'canon.js', 'style.css', '.nojekyll'];
  for (const f of files) assert.ok(existsSync(new URL(`../../${f}`, import.meta.url)), f);
  const wf = readFileSync(new URL('../../../.github/workflows/pages.yml', import.meta.url), 'utf8');
  const copied = wf.match(/for f in ([^;\n]+);\s*do\b/);
  assert.ok(copied, 'workflow copies an explicit file list');
  assert.deepEqual(copied[1].trim().split(/\s+/).sort(), [...files].sort());
  assert.match(wf, /upload-pages-artifact@[0-9a-f]{40}[\s\S]*?path: _pages\n/);
  assert.doesNotMatch(wf, /path:\s*(\.|site\/?)\s*$/m, 'never uploads the repo root or all of site/');
  assert.doesNotMatch(wf, /pull_request|secrets\./);
  assert.match(wf, /group: pages\n\s*cancel-in-progress: false/);
  assert.match(wf, /environment:\s*\n\s*name: github-pages/);
  for (const m of wf.matchAll(/uses:\s*(\S+)@(\S+)/g)) assert.match(m[2], /^[0-9a-f]{40}$/, `${m[1]} pinned by SHA`);
});
