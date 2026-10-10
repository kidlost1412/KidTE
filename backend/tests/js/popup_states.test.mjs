// node --test backend/tests/js
import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {videoSourceBadge as web} from '../../src/web/app/ui/live-status.js';

const require = createRequire(import.meta.url);
const variants = ['../../../extension/popup-ux.js', '../../../extension-shopee/popup-ux.js'].map(p => require(p));

for (const [i, ux] of variants.entries()) {
  const name = i ? 'shopee' : 'tiktok';
  test(`${name}: locked có chữ, class và tuổi đo`, () => {
    const b = ux.videoSourceBadge({state: 'locked', available: true, verification_age_s: 4.2, lock_id: 'x'});
    assert.equal(b.kind, 'locked'); assert.equal(b.className, 'src-locked');
    assert.match(b.text, /Video đã khóa · vị trí đã kiểm chứng · 4 giây trước/);
  });
  test(`${name}: uncertain khác locked`, () => {
    const b = ux.videoSourceBadge({state: 'uncertain', available: false, verification_age_s: 41});
    assert.equal(b.kind, 'uncertain'); assert.equal(b.className, 'src-uncertain');
    assert.match(b.text, /chưa chắc chắn · kiểm chứng cách đây 41 giây/);
  });
  test(`${name}: stale/mất tín hiệu`, () => {
    const b = ux.videoSourceBadge({available: false, reason: 'stale', age_s: 12.4});
    assert.equal(b.kind, 'stale'); assert.equal(b.className, 'src-stale');
    assert.match(b.text, /Mất tín hiệu nguồn · đo cách đây 12 giây/);
  });
  test(`${name}: pin đã xác nhận theo tuổi quan sát`, () => {
    const now = 100000;
    const ok = ux.pinBadge(now - 3000, 'Đạm Cá', now);
    assert.equal(ok.className, 'pin-confirmed'); assert.match(ok.text, /^Pin đã xác nhận · Đạm Cá · quan sát 3 giây trước/);
    const old = ux.pinBadge(now - 40000, 'Đạm Cá', now);
    assert.equal(old.className, 'pin-unconfirmed'); assert.match(old.text, /^Pin chưa xác nhận · lần cuối 40 giây trước/);
    assert.equal(ux.pinBadge(0, '', now).kind, 'unconfirmed');
  });
}

test('web: ba trạng thái', () => {
  assert.deepEqual([web({state: 'locked', available: true, verification_age_s: 2}).kind, web({state: 'uncertain'}).kind, web({available: false, reason: 'stale', age_s: 9}).kind], ['locked', 'uncertain', 'stale']);
  assert.match(web({state: 'locked', available: true, verification_age_s: 2}).text, /đã kiểm chứng · 2 giây trước/);
  assert.match(web({state: 'uncertain', verification_age_s: 30}).className, /warn src-uncertain/);
  assert.match(web({available: false, reason: 'stale', age_s: 9}).text, /9 giây/);
});
