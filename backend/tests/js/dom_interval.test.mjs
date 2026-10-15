// F9: disposer.interval không chồng yêu cầu và bỏ nhịp khi tab ẩn.
import test from 'node:test';
import assert from 'node:assert/strict';
import {disposer} from '../../src/web/app/core/dom.js';

const tick = ms => new Promise(resolve => setTimeout(resolve, ms));

test('interval bỏ nhịp khi lần trước (Promise) chưa xong', async () => {
  globalThis.document = {hidden: false};
  const d = disposer();
  let calls = 0, release;
  d.interval(() => { calls += 1; return new Promise(resolve => { release = resolve; }); }, 10);
  await tick(60);
  assert.equal(calls, 1);
  release();
  await tick(30);
  assert.ok(calls >= 2);
  d.dispose();
  const after = calls;
  release?.();
  await tick(40);
  assert.equal(calls, after);
});

test('interval vẫn chạy tiếp sau khi lần trước bị từ chối', async () => {
  globalThis.document = {hidden: false};
  const d = disposer();
  let calls = 0;
  let rethrown = 0;
  // thenable bị từ chối; lỗi được ném lại cho caller (như trước), ta bắt ở đây.
  const failing = () => ({then(_ok, fail) {
    setTimeout(() => { try { fail(new Error('x')); } catch (_) { rethrown += 1; } }, 0);
  }});
  d.interval(() => { calls += 1; return failing(); }, 10);
  await tick(60);
  d.dispose();
  assert.ok(calls >= 2);
  assert.ok(rethrown >= 1);
});

test('interval bỏ nhịp khi tab ẩn, trừ khi whenHidden', async () => {
  globalThis.document = {hidden: true};
  const d = disposer();
  let normal = 0, always = 0;
  d.interval(() => { normal += 1; }, 10);
  d.interval(() => { always += 1; }, 10, {whenHidden: true});
  await tick(50);
  assert.equal(normal, 0);
  assert.ok(always >= 2);
  globalThis.document.hidden = false;
  await tick(40);
  assert.ok(normal >= 1);
  d.dispose();
});

test('hàm đồng bộ không bị khoá in-flight', async () => {
  globalThis.document = {hidden: false};
  const d = disposer();
  let calls = 0;
  d.interval(() => { calls += 1; }, 10);
  await tick(200);
  d.dispose();
  assert.ok(calls >= 2);
});
