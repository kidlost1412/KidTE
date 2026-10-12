import test from 'node:test';
import assert from 'node:assert/strict';
import {confirmTextFor, deleteProfiles, restoreProfiles, loadDeletedProfiles} from '../../src/web/app/pages/profile-delete-model.js';

test('confirm text names the product and warns about mapped SKUs', () => {
  const one = confirmTextFor([{id: 'SP-1', title: 'Đạm Cá 5L', mapped_skus: 2}]);
  assert.match(one.title, /Đạm Cá 5L/);
  assert.match(one.text, /30 ngày/);
  assert.match(one.text, /2 sản phẩm/);
  const many = confirmTextFor([1, 2, 3, 4, 5].map(i => ({id: 'P' + i, title: 'SP ' + i})));
  assert.match(many.title, /5 hồ sơ/);
  assert.match(many.text, /và 2 hồ sơ khác/);
  assert.doesNotMatch(many.text, /đang có/i);
});

test('delete/restore call the right endpoints and isolate failures', async () => {
  const calls = [];
  const api = async (url, opts) => { calls.push([url, opts]); if (url.includes('bad')) throw new Error('x'); return {ok: true, items: [{resource: 'profiles', record_id: 'a'}, {resource: 'videos'}]}; };
  const out = await deleteProfiles(api, ['a b', 'bad', 'c']);
  assert.deepEqual(out.done, ['a b', 'c']);
  assert.equal(out.failed.length, 1);
  assert.equal(calls[0][0], '/api/workspace/profiles/a%20b');
  assert.equal(calls[0][1].method, 'DELETE');
  const r = await restoreProfiles(api, ['a']);
  assert.equal(calls.at(-1)[0], '/api/workspace/profiles/a/restore');
  assert.deepEqual(r.done, ['a']);
  const del = await loadDeletedProfiles(api);
  assert.equal(calls.at(-1)[0], '/api/trash?resource=profiles&limit=100');
  assert.equal(del.length, 1);
});
