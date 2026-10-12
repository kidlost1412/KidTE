import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
const source = await readFile(new URL('../../src/web/app/core/asset_guard.js', import.meta.url), 'utf8');
const {sheetLoaded, bustUrl, guardStylesheets} = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));

function fakeDoc(links) {
  const doc = {
    head: {appendChild: l => links.push(l)},
    createElement: () => {
      const handlers = {};
      const el = {rel: '', handlers, sheet: null, removed: false,
        set href(v) { this._href = v; }, get href() { return this._href; },
        getAttribute() { return this._href; }, addEventListener(n, f) { handlers[n] = f; },
        after(n) { links.splice(links.indexOf(this) + 1, 0, n); }, remove() { this.removed = true; links.splice(links.indexOf(this), 1); }};
      return el;
    },
    querySelectorAll: () => ({forEach: f => [...links].forEach(f)}),
  };
  return doc;
}
const link = (href, sheet) => ({_href: href, sheet, getAttribute() { return this._href; }, after(n) { this.owner.splice(this.owner.indexOf(this) + 1, 0, n); }, remove() { this.owner.splice(this.owner.indexOf(this), 1); }});

test('sheetLoaded needs a readable, non-empty sheet', () => {
  assert.equal(sheetLoaded({sheet: null}), false);
  assert.equal(sheetLoaded({sheet: {cssRules: []}}), false);
  assert.equal(sheetLoaded({sheet: {cssRules: [1]}}), true);
  assert.equal(sheetLoaded({sheet: {get cssRules() { throw new Error('SecurityError'); }}}), true);
});

test('bustUrl keeps the version query and replaces old retry params', () => {
  assert.equal(bustUrl('/a.css?v=1.2', 1, 5), '/a.css?v=1.2&retry=1&t=5');
  assert.equal(bustUrl('/a.css?v=1.2&retry=1&t=5', 2, 9), '/a.css?v=1.2&retry=2&t=9');
});

test('failed stylesheet is reloaded with cache-busting query, good one untouched', () => {
  const links = [];
  const good = link('/good.css?v=1', {cssRules: [1]}); good.owner = links;
  const bad = link('/bad.css?v=1', null); bad.owner = links;
  links.push(good, bad);
  const timers = [];
  guardStylesheets({doc: fakeDoc(links), retries: 3, delay: 10, setTimer: f => timers.push(f), now: () => 77});
  assert.equal(links.length, 3);
  const fresh = links[2];
  assert.equal(fresh.href, '/bad.css?v=1&retry=1&t=77');
  fresh.sheet = {cssRules: [1]};
  fresh.handlers.load();
  timers.splice(0).forEach(f => f());
  assert.equal(links.includes(bad), false);
  assert.equal(links.includes(fresh), true);
});

test('gives up after the retry limit', () => {
  const links = [];
  const bad = link('/bad.css', null); bad.owner = links; links.push(bad);
  const timers = [];
  const guard = guardStylesheets({doc: fakeDoc(links), retries: 2, delay: 1, setTimer: f => timers.push(f), now: () => 1});
  for (let i = 0; i < 20 && timers.length; i += 1) timers.splice(0).forEach(f => f());
  assert.equal(guard.attempts.get('/bad.css'), 2);
});
