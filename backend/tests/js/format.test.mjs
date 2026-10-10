import test from 'node:test';
import assert from 'node:assert/strict';
import {clock, parseClock, fold, packSize, sizeMismatch, shortDuration} from '../../src/web/app/core/format.js';

test('clock không hiện mili-giây thô, có giờ khi dài', () => {
  assert.equal(clock(167000), '02:47');
  assert.equal(clock(3014536), '50:14');
  assert.equal(clock(3725000), '1:02:05');
  assert.equal(clock(167500, {tenths: true}), '02:47.5');
  assert.equal(clock(-5), '00:00');
});

test('parseClock nhận nhiều cách gõ tự nhiên', () => {
  assert.equal(parseClock('2:47'), 167000);
  assert.equal(parseClock('02:47.5'), 167500);
  assert.equal(parseClock('1:02:05'), 3725000);
  assert.equal(parseClock('167'), 167000);
  assert.equal(parseClock('2p47'), 167000);
  assert.equal(parseClock('2:75'), null);
  assert.equal(parseClock('abc'), null);
});

test('fold bỏ dấu tiếng Việt để tìm kiếm', () => {
  assert.equal(fold('Đạm Cá  Hữu cơ'), 'dam ca huu co');
});

test('phát hiện hồ sơ khác quy cách với listing', () => {
  assert.equal(packSize('Đạm Cá Amino 1L Chất Hữu Cơ'), '1l');
  assert.equal(packSize('ĐẠM CÁ HỮU CƠ AMINO (5 lít)'), '5l');
  assert.equal(packSize('Kích rễ (chai 100ml)'), '100ml');
  assert.equal(packSize('Gói 0,5 kg'), '500g');
  assert.deepEqual(sizeMismatch('Đạm Cá Amino 1L', 'ĐẠM CÁ HỮU CƠ AMINO (5 lít)'), {listing: '1l', profile: '5l'});
  assert.equal(sizeMismatch('Đạm cá can 5 lít', 'ĐẠM CÁ (5 lít)'), null);
  assert.equal(sizeMismatch('Đạm cá', 'ĐẠM CÁ (5 lít)'), null);
  assert.equal(sizeMismatch('SẠCH RONG RÊU Chai 250-500ml', 'NANO ĐỒNG (250ml)'), null, 'listing bán nhiều cỡ có cỡ của hồ sơ');
  assert.deepEqual(sizeMismatch('NANO ĐỒNG (Chai 500ml)', 'NANO ĐỒNG (250ml)'), {listing: '500ml', profile: '250ml'});
});

test('shortDuration gọn cho timeline', () => {
  assert.equal(shortDuration(38000), '38s');
  assert.equal(shortDuration(83000), '1p23');
  assert.equal(shortDuration(3900000), '1g05');
});
