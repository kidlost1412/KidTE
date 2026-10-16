import test from 'node:test';
import assert from 'node:assert/strict';
import {valueText, proposalRows, chosenFields, droppedText, batchText, diagnosticText} from '../../src/web/app/pages/product-enrich-model.js';

test('general flag and diagnostic text', () => {
  const rows = proposalRows({fields: {luu_y: {value: 'x', general_sources: true, sources: [{url: 'https://a.vn', title: 'a.vn'}]}}});
  assert.equal(rows[0].general, true);
  assert.match(diagnosticText({diagnostic: {model: 'gemini-x', chunks: 0, supports: 0, finish_reason: 'STOP'}}), /gemini-x · 0 nguồn Google.*STOP/);
  assert.equal(diagnosticText({}), '');
});

test('value text formats lists and pairs', () => {
  assert.equal(valueText(['a', 'b']), 'a\nb');
  assert.equal(valueText({Abamectin: '36 g/l'}), 'Abamectin: 36 g/l');
  assert.equal(valueText(null), '');
});

test('rows keep only fields with a value and an http source, all checked', () => {
  const rows = proposalRows({fields: {
    cong_dung: {label: 'Công dụng', old: null, value: ['Trừ sâu'], sources: [{url: 'https://a.vn/x', title: 'a.vn'}]},
    luu_y: {label: 'Lưu ý', old: null, value: 'x', sources: [{url: 'javascript:alert(1)', title: 'bad'}]},
  }});
  assert.equal(rows.length, 1);
  assert.equal(rows[0].old, '(đang trống)');
  assert.deepEqual(chosenFields(rows), ['cong_dung']);
  rows[0].checked = false;
  assert.deepEqual(chosenFields(rows), []);
});

test('dropped and batch texts', () => {
  assert.match(droppedText({dropped: {luu_y: 'không có nguồn'}}), /1 mục/);
  assert.equal(droppedText({}), '');
  assert.match(batchText({state: 'running', done: 1, total: 3, current: 'Sâu xanh'}), /2\/3: Sâu xanh/);
  assert.match(batchText({state: 'done', done: 2, total: 2, results: [{ok: true, fields: ['luu_y']}, {ok: false}]}), /1 hồ sơ có đề xuất.*1 hồ sơ lỗi/);
  assert.match(batchText({state: 'cancelled', done: 1, total: 4, results: []}), /Đã dừng/);
});
