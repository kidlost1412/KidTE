import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
const source = (await readFile(new URL('../../src/web/app/pages/video/upload.js', import.meta.url), 'utf8'))
  .replace("import {request, ApiError} from '../../core/api.js';", '');
const {validateFile, MAX_VIDEO_BYTES} = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
test('accepts 50 and 100 GB, rejects larger and invalid sizes before upload', () => {
  assert.equal(MAX_VIDEO_BYTES, 100 * 1024 ** 3);
  for (const size of [50 * 1024 ** 3, MAX_VIDEO_BYTES]) assert.equal(validateFile({name:'large.mp4',size}), '');
  for (const size of [MAX_VIDEO_BYTES + 1, NaN, Infinity]) assert.match(validateFile({name:'large.mp4',size}), /100 GB/);
});

test('resume probe includes size, name and lastModified', async () => {
  const {contentProbe} = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
  const bytes = new Uint8Array(3 * 1024 * 1024).fill(7);
  const mk = (name, lastModified, data = bytes) => Object.assign(new Blob([data]), {name, lastModified});
  const base = await contentProbe(mk('a.mp4', 1000));
  assert.match(base, /^[0-9a-f]{64}$/);
  assert.equal(await contentProbe(mk('a.mp4', 1000)), base);
  assert.notEqual(await contentProbe(mk('b.mp4', 1000)), base);
  assert.notEqual(await contentProbe(mk('a.mp4', 2000)), base);
  const bigger = new Uint8Array(bytes.length + 1).fill(7);
  assert.notEqual(await contentProbe(mk('a.mp4', 1000, bigger)), base);
});
