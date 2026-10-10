import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
const source = await readFile(new URL('../../src/web/app/pages/overview.js', import.meta.url), 'utf8');
const body = source.match(/export function queuePanelModel[\s\S]*?\r?\n}\r?\n/)[0].replace('export ', '');
const queuePanelModel = new Function(body + '; return queuePanelModel;')();

test('queue model highlights unknown and reads mode', () => {
  const m = queuePanelModel({pending: 3, oldest_age_s: 42, expired: 1, unknown: 2, queue_mode: true});
  assert.deepEqual(m, {pending: 3, oldest: 42, expired: 1, unknown: 2, mode: true, warn: true});
  assert.equal(queuePanelModel({pending: 0, unknown: 0}).warn, false);
});
test('missing ai_queue (old backend) renders nothing', () => {
  assert.equal(queuePanelModel(undefined), null);
  assert.equal(queuePanelModel({queue_mode: 'x'}).mode, false);
});
test('overview poll key includes ai_queue so panel refreshes', () => {
  assert.match(source, /dash\.ai_queue/);
});
