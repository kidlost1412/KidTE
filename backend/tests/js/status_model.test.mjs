import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
const rd = rel => readFile(new URL('../../src/web/app/' + rel, import.meta.url), 'utf8');
const {connectionLost, shortPill, OFFLINE_LABEL} = await import('data:text/javascript;base64,' + Buffer.from(await rd('core/status-model.js')).toString('base64'));

test('mất kết nối hiện kể cả khi còn dashboard cũ', () => {
  assert.equal(connectionLost(new Error('x')), true);
  assert.equal(connectionLost(null), false);
  assert.match(OFFLINE_LABEL, /Mất kết nối/);
});

test('shell dùng connectionLost, không còn điều kiện error && !dash', async () => {
  const shell = await rd('shell.js');
  assert.match(shell, /connectionLost\(error\)/);
  assert.doesNotMatch(shell, /error && !dash/);
});

test('nhãn rút gọn có chữ, không chỉ màu', () => {
  assert.equal(shortPill('AI', 'ok'), 'AI ✓');
  assert.equal(shortPill('TikTok', 'warn'), 'TikTok …');
  assert.equal(shortPill('CAPTCHA', 'danger'), 'CAPTCHA ✗');
});
