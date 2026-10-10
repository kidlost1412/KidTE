import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
let source = await readFile(new URL('../../src/web/app/core/store.js', import.meta.url), 'utf8');
source = source.replace("import {request} from './api.js';", 'const request = (...args) => globalThis.kidtePollRequest(...args);');
const {createStore, startDashboardPoll} = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));

test('dashboard refresh coalesces while request runs and recovers after error', async () => {
  const oldDocument = globalThis.document;
  globalThis.document = {hidden: false};
  const pending = [];
  globalThis.kidtePollRequest = () => new Promise((resolve, reject) => pending.push({resolve, reject}));
  const store = createStore();
  const poll = startDashboardPoll(store, 10000);
  try {
    for (let i = 0; i < 30; i++) poll.refresh();
    assert.equal(pending.length, 1);
    pending[0].reject({code: 'network_error'});
    await new Promise(resolve => setTimeout(resolve, 10));
    assert.equal(pending.length, 2);
    pending[1].resolve({ok: true, marker: 1});
    await new Promise(resolve => setTimeout(resolve, 0));
    assert.equal(store.get().dashboard.marker, 1);
    assert.equal(store.get().dashboardError, null);
    poll.refresh();
    assert.equal(pending.length, 3);
    poll.stop();
    pending[2].resolve({ok: true, marker: 2});
    await new Promise(resolve => setTimeout(resolve, 0));
    assert.equal(store.get().dashboard.marker, 1);
  } finally {
    poll.stop();
    globalThis.document = oldDocument;
    delete globalThis.kidtePollRequest;
  }
});
