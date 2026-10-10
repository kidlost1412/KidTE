import test from 'node:test';
import assert from 'node:assert/strict';
import {healthModel, selftestRows, ago} from '../../src/web/app/pages/captcha-model.js';

const now = new Date('2026-10-05T10:01:10');
test('healthModel shows label, pid and stats', () => {
  const m = healthModel({running: true, pid: 7, admin: true, health: {
    state: 'service_error', label: 'Lỗi dịch vụ giải: HTTP 400', tone: 'warn', attempts_today: 3,
    last_heartbeat_at: '2026-10-05 10:01:00', last_scan_resolution: '2560x1440',
    last_api_error: 'HTTP 400', last_api_error_at: '2026-10-05 10:00:11'}}, now);
  assert.equal(m.tone, 'warn');
  assert.match(m.sub, /PID 7 · quyền Administrator/);
  assert.equal(m.stats.length, 4);
  assert.match(m.stats[0].value, /10 giây trước · 2560x1440/);
  assert.equal(m.stats[2].value, '3');
  assert.match(m.stats[3].value, /HTTP 400 \(10:00\)/);
});
test('healthModel falls back without health', () => {
  assert.equal(healthModel({running: false}).label, 'Chưa chạy');
  assert.equal(ago(null), '—');
});
test('selftestRows handles failures', () => {
  assert.equal(selftestRows({error: 'captcha_selftest_timeout', checks: []})[0].ok, false);
  const rows = selftestRows({checks: [{name: 'ffmpeg', ok: true}, {name: 'zzz', ok: false, hint: 'x'}]});
  assert.deepEqual(rows.map(r => [r.name, r.ok]), [['FFmpeg (quay video)', true], ['zzz', false]]);
});
