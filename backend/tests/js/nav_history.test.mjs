import test from 'node:test';
import assert from 'node:assert/strict';
import {createHistoryStack, normalizeHash} from '../../src/web/app/core/nav_history.js';

test('normalizeHash đưa trang trống về trang tổng quan', () => {
  assert.equal(normalizeHash(''), '#overview');
  assert.equal(normalizeHash('#'), '#overview');
  assert.equal(normalizeHash('#/'), '#overview');
  assert.equal(normalizeHash('#inbox/unanswered'), '#inbox/unanswered');
});

test('mới mở ứng dụng chưa có gì để quay lại', () => {
  const stack = createHistoryStack();
  assert.equal(stack.canBack(), false);
  assert.equal(stack.previous(), null);
  assert.equal(stack.pop(), null);
  stack.visit('#overview');
  assert.equal(stack.canBack(), false);
  assert.equal(stack.current(), '#overview');
});

test('ghi nhận các trang đã đi qua theo thứ tự, gồm cả sub-tab', () => {
  const stack = createHistoryStack();
  stack.visit('#overview');
  stack.visit('#inbox');
  stack.visit('#inbox/unanswered');
  stack.visit('#catalog/mapping');
  assert.deepEqual(stack.snapshot(), ['#overview', '#inbox', '#inbox/unanswered', '#catalog/mapping']);
  assert.equal(stack.canBack(), true);
  assert.equal(stack.previous(), '#inbox/unanswered');
});

test('trùng liên tiếp với trang đang xem thì không ghi thêm', () => {
  const stack = createHistoryStack();
  stack.visit('#overview');
  stack.visit('#overview');
  stack.visit('#');
  assert.deepEqual(stack.snapshot(), ['#overview']);
});

test('quay lại lần lượt rồi dừng khi hết lịch sử', () => {
  const stack = createHistoryStack();
  ['#overview', '#inbox', '#video'].forEach(h => stack.visit(h));
  assert.equal(stack.pop(), '#inbox');
  assert.equal(stack.pop(), '#overview');
  assert.equal(stack.canBack(), false);
  assert.equal(stack.pop(), null);
  assert.deepEqual(stack.snapshot(), ['#overview']);
});

test('đi tới trang mới sau khi quay lại thì ghi tiếp từ vị trí hiện tại', () => {
  const stack = createHistoryStack();
  ['#overview', '#inbox', '#video'].forEach(h => stack.visit(h));
  stack.pop(); // đang ở #inbox
  stack.visit('#brain');
  assert.deepEqual(stack.snapshot(), ['#overview', '#inbox', '#brain']);
  assert.equal(stack.previous(), '#inbox');
});

test('replaceCurrent đổi trang đang xem mà không tạo mục mới', () => {
  const stack = createHistoryStack();
  stack.visit('#video/library');
  stack.visit('#video/library/abc');
  stack.replaceCurrent('#video/library/def');
  assert.deepEqual(stack.snapshot(), ['#video/library', '#video/library/def']);
  assert.equal(stack.previous(), '#video/library');
});

test('replaceCurrent khi ngăn xếp rỗng tạo mục đầu tiên', () => {
  const stack = createHistoryStack();
  stack.replaceCurrent('');
  assert.deepEqual(stack.snapshot(), ['#overview']);
});

test('giới hạn độ dài: bỏ mục cũ nhất', () => {
  const stack = createHistoryStack({limit: 3});
  ['#a', '#b', '#c', '#d', '#e'].forEach(h => stack.visit(h));
  assert.deepEqual(stack.snapshot(), ['#c', '#d', '#e']);
  assert.equal(stack.size(), 3);
  assert.equal(stack.pop(), '#d');
  assert.equal(stack.pop(), '#c');
  assert.equal(stack.canBack(), false);
});

test('mô phỏng nút quay lại: đi A → B → C, quay lại hai lần về A', () => {
  const stack = createHistoryStack();
  stack.visit('#overview');
  stack.visit('#inbox');
  stack.visit('#customers');
  // Người dùng bấm quay lại: router đến previous() rồi gọi pop() khi đã tới đó.
  assert.equal(stack.previous(), '#inbox');
  assert.equal(stack.pop(), '#inbox');
  assert.equal(stack.previous(), '#overview');
  assert.equal(stack.pop(), '#overview');
  assert.equal(stack.canBack(), false);
});
