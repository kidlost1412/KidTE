import test from 'node:test';
import assert from 'node:assert/strict';
import {liveMismatchWarning, pickerCartKey, cartLockNote} from '../../src/web/app/pages/product-list-model.js';

const carts = [
  {key: 'default', name: 'Mặc định', live: true, active: false},
  {key: 'uid:222', name: '@shopb', unique_id: 'shopb', live: false, active: true},
];

test('picker shows the cart the list is using (active), not a stale selected value', () => {
  assert.equal(pickerCartKey({active: 'uid:222', selected: 'default', carts}), 'uid:222');
  assert.equal(pickerCartKey({active: 'gone', selected: 'default', carts}), 'default');
  assert.equal(pickerCartKey({carts}), 'default');
  assert.equal(pickerCartKey(null), '');
});

test('warning when LIVE account differs from the cart AI uses', () => {
  const state = {live: 'uid:222', live_name: '@shopb', active: 'default', active_name: 'Mặc định', selected_explicit: true,
    carts: [{...carts[0], live: false, active: true}, {...carts[1], live: true, active: false}]};
  const warn = liveMismatchWarning(state);
  assert.equal(warn.title, 'Đang LIVE @shopb nhưng AI đang dùng giỏ «Mặc định»');
  assert.equal(warn.action, 'Dùng giỏ của tài khoản LIVE');
  assert.match(cartLockNote(state), /Mặc định/);
});

test('cart chosen in Studio for the playing video is named, without a useless button', () => {
  const state = {live: 'uid:222', live_name: '@shopb', active: 'scan:b', active_name: 'Giỏ B', active_source: 'video',
    selected: 'default', selected_explicit: true,
    carts: [...carts, {key: 'scan:b', name: 'Giỏ B', live: false, active: true}]};
  assert.equal(cartLockNote(state), 'Buổi LIVE đang dùng giỏ «Giỏ B» (theo video trong Studio).');
  const warn = liveMismatchWarning(state);
  assert.match(warn.title, /«Giỏ B» \(theo video trong Studio\)/);
  assert.equal(warn.action, '');
});

test('no warning when not live or AI uses the live cart', () => {
  assert.equal(liveMismatchWarning({live: '', active: 'default', carts}), null);
  assert.equal(liveMismatchWarning({live: 'default', active: 'default', carts}), null);
  assert.equal(liveMismatchWarning(null), null);
});
