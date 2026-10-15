import test from 'node:test';
import assert from 'node:assert/strict';
import {aiStatus} from '../../src/web/app/core/ai-status.js';

test('deepseek_balance_checking là trung tính, không phải lỗi', () => {
  const s = aiStatus({ok: false, deepseek_available: false, reason: 'deepseek_balance_checking'});
  assert.equal(s.kind, 'warn');
  assert.ok(s.checking);
  assert.match(s.text, /Đang kiểm tra số dư DeepSeek/);
});

test('sẵn sàng và lỗi thật giữ nguyên', () => {
  assert.equal(aiStatus({deepseek_available: true}).kind, 'ok');
  assert.equal(aiStatus({ok: false, reason: 'deepseek_timeout'}).kind, 'danger');
  assert.equal(aiStatus(undefined).kind, 'danger');
});
