import test from 'node:test';
import assert from 'node:assert/strict';
import {shortProductName, selectionState, setAllVisible, bulkResultText, bulkRestoreText, bulkDraftText, bulkApproveText} from '../../src/web/app/pages/product-list-model.js';

test('short name cuts at the first marketing phrase and separator', () => {
  assert.equal(shortProductName('Men vi sinh Bacillus 1kg, giúp tăng trưởng rễ bền vững'), 'Men vi sinh Bacillus 1kg');
  assert.equal(shortProductName('Đạm Cá Amino 5L - Chuyên dụng cho cây trồng'), 'Đạm Cá Amino 5L');
  assert.equal(shortProductName('Phân bón lá Zinc | hàng chính hãng'), 'Phân bón lá Zinc');
});

test('short name keeps the size and caps length with an ellipsis', () => {
  const long = 'Phân ' + 'bón'.repeat(30) + ' 500ml';
  const out = shortProductName(long, 60);
  assert.ok(out.length <= 61, out);
  assert.equal(shortProductName('Một hai ba bốn năm sáu bảy tám chín mười mười một mười hai'), 'Một hai ba bốn năm sáu bảy');
  assert.match(out, /…$/);
  assert.equal(shortProductName(''), '');
  assert.equal(shortProductName('Phân bón lá Zinc 500ml'), 'Phân bón lá Zinc 500ml');
});

test('select-all state follows the visible list only', () => {
  const picked = new Set(['a', 'x']);
  assert.deepEqual(selectionState(['a', 'b'], picked), {total: 2, count: 1, all: false, some: true});
  assert.deepEqual(selectionState(['a'], picked), {total: 1, count: 1, all: true, some: false});
  assert.deepEqual(selectionState([], picked), {total: 0, count: 0, all: false, some: false});
});

test('setAllVisible adds or removes only the visible ids and keeps the rest', () => {
  const picked = new Set(['x']);
  setAllVisible(picked, ['a', 'b'], true);
  assert.deepEqual([...picked].sort(), ['a', 'b', 'x']);
  setAllVisible(picked, ['a', 'b'], false);
  assert.deepEqual([...picked], ['x']);
});

test('bulk result text always carries the counts', () => {
  assert.equal(bulkResultText('Đã khôi phục', 3, 0), 'Đã khôi phục 3 hồ sơ.');
  assert.equal(bulkResultText('Đã khôi phục', 2, 1), 'Đã khôi phục 2 hồ sơ; 1 hồ sơ chưa được xử lý.');
});

test('confirm dialogs for bulk actions show the count', () => {
  assert.match(bulkRestoreText(1).title, /1 hồ sơ/);
  assert.match(bulkRestoreText(4).title, /4 hồ sơ/);
  assert.match(bulkDraftText(5).title, /5 hồ sơ nháp/);
  assert.match(bulkApproveText(2).title, /2 hồ sơ/);
  assert.match(bulkApproveText(2).text, /AI/);
});
