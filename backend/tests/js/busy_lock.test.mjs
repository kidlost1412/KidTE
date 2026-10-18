import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
const src = await readFile(new URL('../../src/web/app/core/busy.js', import.meta.url), 'utf8');
const {withBusyLock} = await import('data:text/javascript;base64,' + Buffer.from(src).toString('base64'));

function fakeButton(disabled = false) {
  const cls = new Set(), attrs = new Map();
  return {disabled, dataset: {}, classList: {add: c => cls.add(c), remove: c => cls.delete(c), has: c => cls.has(c)},
    setAttribute: (k, v) => attrs.set(k, v), removeAttribute: k => attrs.delete(k), attrs};
}

test('async onClick khoá nút rồi mở lại khi xong; bấm đúp chỉ chạy 1 lần', async () => {
  let calls = 0, release;
  const handler = withBusyLock(() => { calls++; return new Promise(r => { release = r; }); });
  const b = fakeButton();
  const p = handler({currentTarget: b});
  assert.equal(b.disabled, true);
  assert.equal(b.attrs.get('aria-busy'), 'true');
  handler({currentTarget: b});
  assert.equal(calls, 1);
  release(); await p;
  assert.equal(b.disabled, false);
  assert.equal(b.classList.has('busy'), false);
  handler({currentTarget: b}); assert.equal(calls, 2);
});

test('mở lại cả khi lỗi, và không mở nút vốn đã bị vô hiệu', async () => {
  const b = fakeButton();
  const bad = withBusyLock(async () => { throw new Error('boom'); });
  await assert.rejects(bad({currentTarget: b}), /boom/);
  assert.equal(b.disabled, false);
  const b2 = fakeButton(true);
  await withBusyLock(async () => {})({currentTarget: b2});
  assert.equal(b2.disabled, true);
});

test('onClick đồng bộ không bị khoá', () => {
  const b = fakeButton();
  assert.equal(withBusyLock(() => 5)({currentTarget: b}), 5);
  assert.equal(b.disabled, false);
});
