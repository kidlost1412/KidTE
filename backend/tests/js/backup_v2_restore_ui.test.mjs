import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {restoreV2Visible, confirmProblem, restoreErrorText, restoreV2, reenableAi}
  from '../../src/web/app/pages/backup-v2.js';

test('restore action hidden only when status explicitly reports it disabled', () => {
  assert.equal(restoreV2Visible({}), true);
  assert.equal(restoreV2Visible(null), true);
  assert.equal(restoreV2Visible({restore_v2_enabled: true}), true);
  assert.equal(restoreV2Visible({restore_v2_enabled: false}), false);
  assert.equal(restoreV2Visible({restore_v2: false}), false);
});

test('strong confirmation requires the exact backup id', () => {
  assert.ok(confirmProblem('', 'bk-1'));
  assert.ok(confirmProblem('bk-2', 'bk-1'));
  assert.ok(confirmProblem('BK-1', 'bk-1'));
  assert.equal(confirmProblem(' bk-1 ', 'bk-1'), '');
});

test('restoreV2 refuses without confirmation or password and never calls the API', async () => {
  let calls = 0;
  const request = async () => { calls++; return {}; };
  await assert.rejects(restoreV2({request, backupId: 'bk-1', password: 'long-password', typed: 'nope'}), {code: 'confirmation_mismatch'});
  await assert.rejects(restoreV2({request, backupId: 'bk-1', password: 'short', typed: 'bk-1'}), {code: 'backup_password_required'});
  assert.equal(calls, 0);
});

test('restoreV2 posts v2 payload, reports progress and AI stays disabled', async () => {
  const calls = [], phases = [];
  const request = async (path, opt) => { calls.push([path, opt.method, opt.body]);
    return {ok: true, restored: 'bk-1', ai_enabled: false, auto_pin_enabled: false, rollback_dir: 'C:/x/data.rollback-1'}; };
  const out = await restoreV2({request, backupId: 'bk-1', password: 'long-password', typed: 'bk-1', onPhase: t => phases.push(t)});
  assert.deepEqual(calls, [['/api/backup/restore', 'POST', {v2: true, backup_id: 'bk-1', password: 'long-password'}]]);
  assert.equal(phases.length, 2);
  assert.match(phases[0], /bảo trì/);
  assert.deepEqual(out, {restored: 'bk-1', aiEnabled: false, autoPinEnabled: false, rollbackDir: 'C:/x/data.rollback-1'});
});

test('reenableAi calls the re-enable route', async () => {
  const calls = [];
  await reenableAi({request: async (path, opt) => { calls.push([path, opt.method]); return {ok: true}; }});
  assert.deepEqual(calls, [['/api/backup/restore/reenable-ai', 'POST']]);
});

test('restore_v2_disabled and rollback errors are explained clearly', () => {
  assert.match(restoreErrorText({code: 'restore_v2_disabled'}), /chưa được bật/);
  assert.doesNotMatch(restoreErrorText({code: 'restore_v2_disabled'}), /restore_v2|\.json/);
  assert.match(restoreErrorText({code: 'maintenance_required'}), /LIVE/);
  assert.match(restoreErrorText({code: 'restore_failed/RuntimeError'}), /quay về dữ liệu cũ/);
  assert.match(restoreErrorText({code: 'writers_close_failed/learning'}), /nguyên vẹn/);
  assert.ok(restoreErrorText(new Error('weird')).length > 0);
});

test('backups page wires the restore action, AI-disabled notice and reenable button', async () => {
  const src = await readFile(new URL('../../src/web/app/pages/system.js', import.meta.url), 'utf8');
  assert.match(src, /restoreV2Visible\(dash\)/);
  assert.match(src, /data-restore-v2/);
  assert.match(src, /Bật lại AI/);
  assert.match(src, /confirmProblem\(v, b\.id\)/);
});
