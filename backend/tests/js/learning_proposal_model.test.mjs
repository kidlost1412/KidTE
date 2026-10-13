import test from 'node:test';
import assert from 'node:assert/strict';
import {changeIds, keptCount, applyLabel, acceptedPayload, rowChecked} from '../../src/web/app/pages/learning-proposal-model.js';

const OUTPUT = {
  items: [
    {action: 'keep', from_ids: ['a']},
    {action: 'merge', from_ids: ['b', 'c']},
    {action: 'revise', from_ids: ['d']},
    {action: 'new', from_ids: []},
  ],
  retire_ids: ['e', 'e', 'f'],
  personas: [{persona_id: 'default'}, {persona_id: 'p2'}],
};

test('changeIds lists non-keep items, unique retires and styles in display order', () => {
  assert.deepEqual(changeIds(OUTPUT), [
    'item:1', 'item:2', 'item:3', 'retire:e', 'retire:f', 'style:default', 'style:p2',
  ]);
});

test('changeIds tolerates empty or missing output', () => {
  assert.deepEqual(changeIds(null), []);
  assert.deepEqual(changeIds({}), []);
});

test('apply label counts kept proposals out of the total', () => {
  const ids = changeIds(OUTPUT);
  assert.equal(ids.length, 7);
  assert.equal(applyLabel(ids, new Set()), 'Áp dụng 7/7 đề xuất');
  assert.equal(applyLabel(ids, new Set(['item:1', 'style:p2'])), 'Áp dụng 5/7 đề xuất');
});

test('acceptedPayload sends only kept ids, in order', () => {
  const ids = changeIds(OUTPUT);
  assert.deepEqual(acceptedPayload(ids, new Set(['item:2', 'retire:e'])),
    ['item:1', 'item:3', 'retire:f', 'style:default', 'style:p2']);
  assert.equal(keptCount(ids, new Set(ids)), 0);
});

test('rowChecked follows the dropped set while editable, and the saved result afterwards', () => {
  const dropped = new Set(['item:1']);
  assert.equal(rowChecked('item:1', {editable: true, dropped, applied: new Set()}), false);
  assert.equal(rowChecked('item:2', {editable: true, dropped, applied: new Set()}), true);
  assert.equal(rowChecked('item:1', {editable: false, dropped: new Set(), applied: new Set(['item:2'])}), false);
  assert.equal(rowChecked('item:2', {editable: false, dropped: new Set(), applied: new Set(['item:2'])}), true);
});
