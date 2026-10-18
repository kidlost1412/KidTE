import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
const source = await readFile(new URL('../../src/web/app/core/api.js', import.meta.url), 'utf8');
const {request} = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));

test('request timeout includes delayed body and preserves caller cancellation', async () => {
  const previous = globalThis.fetch;
  try {
    globalThis.fetch = async (_url, config) => ({ok: true, json: () => new Promise((_resolve, reject) => {
      config.signal.addEventListener('abort', () => reject(new DOMException('abort', 'AbortError')), {once: true});
    })});
    await assert.rejects(request('/test', {timeoutMs: 5}), error => error.code === 'timeout');
    const controller = new AbortController();
    const pending = request('/test', {signal: controller.signal});
    await new Promise(resolve => setTimeout(resolve, 0));
    controller.abort();
    await assert.rejects(pending, error => error.code === 'aborted');
    globalThis.fetch = async () => ({ok: true, json: async () => ({ok: true})});
    assert.deepEqual(await request('/test'), {ok: true});
  } finally {globalThis.fetch = previous;}
});

test('server conflict is retained for caller instead of network error', async () => {
  const previous = globalThis.fetch;
  try {
    globalThis.fetch = async () => ({ok: false, status: 409,
      json: async () => ({ok: false, error: 'revision_conflict'})});
    await assert.rejects(request('/test'), error => error.code === 'revision_conflict' && error.status === 409);
  } finally {globalThis.fetch = previous;}
});

test('friendlyError: mã chưa biết dùng câu chung + mã nhỏ, ưu tiên message của server', async () => {
  const {friendlyError, GENERIC_ERROR} = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
  const unknown = friendlyError({code: 'document_not_found_xyz'});
  assert.ok(unknown.startsWith(GENERIC_ERROR));
  assert.match(unknown, /\(mã: document_not_found_xyz\)/);
  assert.doesNotMatch(unknown, /^Không thực hiện được/);
  assert.equal(friendlyError({code: 'xyz_unknown', payload: {message: 'Câu riêng từ máy chủ.'}}), 'Câu riêng từ máy chủ.');
  for (const code of ['insufficient_download_space', 'package_size_mismatch', 'catalog_http_403', 'catalog_http_429', 'package_http_error', 'empty_catalog', 'package_resume_rejected', 'invalid_signature']) {
    const text = friendlyError({code});
    assert.doesNotMatch(text, /mã:/, code);
    assert.match(text, /[ạảãàáâăêôơưđ]/i, code);
  }
});
