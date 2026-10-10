import test from 'node:test';
import assert from 'node:assert/strict';
import {kindLabel, normalizeV2, passwordProblem, readIndexedDb, exportIndexedDb, startV2}
  from '../../src/web/app/pages/backup-v2.js';

test('full and partial are labelled differently and normalised', () => {
  assert.notEqual(kindLabel('full'), kindLabel('partial'));
  const rows = normalizeV2([{id: 'a', kind: 'full', size: '5', verified: true}, {id: 'b', kind: 'weird'}, null, {kind: 'full'}]);
  assert.deepEqual(rows.map(r => [r.id, r.kind, r.size, r.verified]), [['a', 'full', 5, true], ['b', 'partial', 0, false]]);
});

test('password prompt validation', () => {
  assert.ok(passwordProblem('short'));
  assert.equal(passwordProblem('long-enough-1'), '');
});

test('startV2 posts full flag and password then waits for the job', async () => {
  const calls = [];
  const request = async (path, opt) => { calls.push([path, opt.body]); return {job_id: 'j1'}; };
  const waitJob = async id => ({complete: false, id});
  const out = await startV2({request, waitJob, password: 'pw-123456', full: false});
  assert.deepEqual(calls, [['/api/backup/v2', {full: false, password: 'pw-123456'}]]);
  assert.equal(out.id, 'j1');
});

function fakeIdb() {
  const rows = [{snapshot_id: 's1', n: 1}];
  return {open() {
    const req = {};
    queueMicrotask(() => {
      req.result = {version: 2, objectStoreNames: ['snapshots'], close() {},
        transaction() {
          const tx = {objectStore: () => ({getAll() {
            const r = {};
            queueMicrotask(() => { r.result = rows; r.onsuccess(); queueMicrotask(() => tx.oncomplete()); });
            return r;
          }})};
          return tx;
        }};
      req.onsuccess();
    });
    return req;
  }};
}

test('IndexedDB recovery store exports to the backend endpoint', async () => {
  const data = await readIndexedDb(fakeIdb());
  assert.equal(data.database, 'kid-te-studio-recovery');
  assert.deepEqual(data.stores.snapshots, [{snapshot_id: 's1', n: 1}]);
  let sent;
  await exportIndexedDb({idb: fakeIdb(), request: async (p, o) => { sent = [p, o]; return {ok: true}; }});
  assert.equal(sent[0], '/api/backup/indexeddb');
  assert.equal(sent[1].method, 'POST');
  assert.equal(sent[1].body.export.stores.snapshots.length, 1);
});

import {selectionIds, deleteV2} from '../../src/web/app/pages/backup-v2.js';

test('selectionIds picks chosen or all known ids', () => {
  const items = [{id: 'a'}, {id: 'b'}, {id: 'c'}];
  assert.deepEqual(selectionIds(items, new Set(['b', 'zzz'])), ['b']);
  assert.deepEqual(selectionIds(items, new Set(), true), ['a', 'b', 'c']);
});

test('deleteV2 previews, shows reclaim size to confirm, then deletes with token', async () => {
  const calls = [];
  const request = async (path, opt) => {
    calls.push([path, opt.body]);
    return path.endsWith('/preview') ? {token: 't1', reclaim_bytes: 4096, ids: ['a', 'b']} : {deleted_ids: ['a', 'b']};
  };
  let shown;
  const out = await deleteV2({request, ids: ['a', 'b'], confirm: async c => { shown = c.reclaim_bytes; return true; }});
  assert.equal(shown, 4096);
  assert.deepEqual(calls, [['/api/backup/v2/delete/preview', {ids: ['a', 'b']}], ['/api/backup/v2/delete', {ids: ['a', 'b'], token: 't1'}]]);
  assert.deepEqual(out.deleted_ids, ['a', 'b']);
});

test('deleteV2 does nothing when cancelled or empty', async () => {
  const calls = [];
  const request = async (path) => { calls.push(path); return {token: 't', reclaim_bytes: 1, ids: ['a']}; };
  assert.equal(await deleteV2({request, ids: ['a'], confirm: async () => false}), null);
  assert.equal(calls.length, 1);
  assert.equal(await deleteV2({request, ids: [], confirm: async () => true}), null);
  assert.equal(calls.length, 1);
});
