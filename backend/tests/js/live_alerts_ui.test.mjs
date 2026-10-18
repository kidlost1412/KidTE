import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
const rd = rel => readFile(new URL('../../src/web/app/' + rel, import.meta.url), 'utf8');
const load = async rel => import('data:text/javascript;base64,' + Buffer.from(await rd(rel)).toString('base64'));

test('O3: báo động AI làm pill AI đỏ dù cache số dư còn "sẵn sàng"', async () => {
  const {aiStatus, liveAlertBanners} = await load('core/ai-status.js');
  const alerts = [{code: 'comments_silent', severity: 'warning', message: 'Không nhận được bình luận TikTok 6 phút — kiểm tra tiện ích/tải lại trang.'},
    {code: 'ai_out_of_balance', severity: 'error', message: 'Hết tiền', action: 'Nạp'}];
  const st = aiStatus({deepseek_available: true}, alerts);
  assert.equal(st.kind, 'danger');
  assert.equal(st.text, 'Hết tiền');
  assert.equal(aiStatus({deepseek_available: true}, []).kind, 'ok');
  const banners = liveAlertBanners(alerts);
  assert.equal(banners[0].tone, 'danger');
  assert.equal(banners[1].tone, 'warn');
});

test('api.js có câu tiếng Việt cho lỗi tải gói mới', async () => {
  const {friendlyError, GENERIC_ERROR} = await load('core/api.js');
  for (const code of ['package_connection_reset', 'package_network_timeout', 'package_http_404', 'package_http_429',
    'package_http_500', 'package_http_502', 'package_http_503', 'package_http_504']) {
    assert.ok(!friendlyError({code}).startsWith(GENERIC_ERROR), code);
  }
});

test('trang Cập nhật có băng lỗi cập nhật + nút Đã hiểu', async () => {
  const src = await rd('pages/updates.js');
  assert.match(src, /last_update_failure/);
  assert.match(src, /\/api\/updates\/ack_update_failure/);
  assert.match(src, /Đã hiểu/);
});
