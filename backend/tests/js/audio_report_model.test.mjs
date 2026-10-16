import test from 'node:test';
import assert from 'node:assert/strict';
import {audioProblems, compactReport} from '../../src/web/app/pages/audio-report-model.js';

test('each error code yields a specific Vietnamese message', () => {
  assert.match(audioProblems({error_code: 'audio_service_stopped'})[0].title, /Dịch vụ âm thanh/);
  assert.match(audioProblems({error_code: 'no_active_output'})[0].title, /loa\/tai nghe/);
  assert.match(audioProblems({error_code: 'enumeration_failed', error_detail: 'OSError 0x80070005'})[0].text, /0x80070005/);
  assert.match(audioProblems({error_code: 'soundcard_unavailable'})[0].title, /Thiếu/);
  assert.equal(audioProblems({error_code: 'ok'}).length, 0);
});

test('old build warns only in process mode; mic deny and missing devices', () => {
  assert.match(audioProblems({windows_build: 18363}, 'process')[0].title, /18363/);
  assert.equal(audioProblems({windows_build: 18363}, 'device').length, 0);
  assert.equal(audioProblems({windows_build: 19041}, 'process').length, 0);
  assert.match(audioProblems({mic_privacy: 'Deny'})[0].title, /micro/);
  assert.match(audioProblems({missing_saved_devices: [{id: 'x'}]})[0].title, /không còn trên máy này/);
});

test('compact report drops device ids', () => {
  const out = JSON.stringify(compactReport({devices: [{id: '{guid}', name: 'Loa', loopback: true}], suggested_device: {id: '{g}', name: 'Loa'}}));
  assert.ok(!out.includes('guid') && !out.includes('{g}'));
});
