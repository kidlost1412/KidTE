import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
let source = await readFile(new URL('../../src/web/app/core/jobs.js', import.meta.url), 'utf8');
source = source.replace("import {request} from './api.js';", 'const request = (...args) => globalThis.kidteJobRequest(...args);');
const {waitJob} = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));

test('job poll is sequential and returns verified result', async () => {
  let active = 0, maximum = 0, calls = 0;
  globalThis.kidteJobRequest = async () => {
    active++; maximum = Math.max(maximum, active);
    await new Promise(resolve => setTimeout(resolve, 2));
    active--;
    return ++calls === 3 ? {state: 'completed', result: {deleted_ids: ['one']}} : {state: 'running', progress: .5};
  };
  assert.deepEqual(await waitJob('id', {intervalMs: 1}), {deleted_ids: ['one']});
  assert.equal(maximum, 1);
});

test('abandoning UI stops poll and interrupted job is never success', async () => {
  const controller = new AbortController();
  let calls = 0;
  globalThis.kidteJobRequest = async () => {calls++; return {state: 'running'};};
  const task = waitJob('id', {signal: controller.signal, intervalMs: 1000});
  await new Promise(resolve => setTimeout(resolve, 0));
  controller.abort();
  await assert.rejects(task, error => error.name === 'AbortError');
  assert.equal(calls, 1);
  globalThis.kidteJobRequest = async () => ({state: 'interrupted'});
  await assert.rejects(waitJob('id'), /gián đoạn/);
});
