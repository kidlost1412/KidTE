import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {doseRulesModel, loadDoseRules, reviewDoseRule, importLabel, ONLY_APPROVED_WARNING} from '../../src/web/app/pages/dose-rules-model.js';

const RULES = [
  {rule_id: 'r1', sku: 'SP-03', amount: 10, unit: 'ml', per_volume: 16, per_unit: 'lít', status: 'candidate', source_doc: 'nhan.pdf', source_page: '2'},
  {rule_id: 'r2', sku: 'SP-03', amount: 20, unit: 'ml', per_volume: 16, per_unit: 'lít', status: 'approved', source_doc: 'nhan.pdf'},
  {rule_id: 'r3', sku: 'SP-03', amount: 5, unit: 'g', per_volume: 8, status: 'rejected'},
];

test('model shows chips, source, needs_review and review permission', () => {
  const m = doseRulesModel(RULES);
  assert.deepEqual([m.candidate, m.approved, m.rejected], [1, 1, 1]);
  assert.equal(m.rows[0].chip.label, 'Chờ duyệt');
  assert.equal(m.rows[0].needsReview, true);
  assert.equal(m.rows[0].canReview, true);
  assert.equal(m.rows[0].source, 'nhan.pdf · tr. 2');
  assert.equal(m.rows[1].canReview, false);
  assert.equal(m.rows[2].source, 'không rõ nguồn');
  assert.equal(m.rows[0].text, '10ml cho 16 lít');
  assert.match(m.warning, /ĐÃ DUYỆT/);
  assert.equal(doseRulesModel([]).noApproved, true);
  assert.equal(doseRulesModel(undefined).rows.length, 0);
});

test('load/review/import call the right endpoints', async () => {
  const calls = [];
  const api = async (url, opts) => { calls.push([url, opts]); return url.includes('import-label') ? {imported: 2, candidates: 2, needs_review: true} : {rules: RULES}; };
  const m = await loadDoseRules(api, 'SP 03');
  assert.equal(calls[0][0], '/api/workspace/dose-rules?sku=SP%2003');
  assert.equal(m.rows.length, 3);
  await reviewDoseRule(api, 'r1', 'approve');
  assert.equal(calls[1][0], '/api/workspace/dose-rules/r1/review');
  assert.deepEqual(calls[1][1], {method: 'POST', body: {decision: 'approve'}});
  await assert.rejects(() => reviewDoseRule(api, 'r1', 'maybe'));
  const r = await importLabel(api, {sku: 'SP-03', text: 'Pha 10ml cho 16 lít; 20ml cho 16 lít', sourceDoc: 'nhan.jpg'});
  assert.deepEqual(calls[2][1].body, {sku: 'SP-03', text: 'Pha 10ml cho 16 lít; 20ml cho 16 lít', source_doc: 'nhan.jpg'});
  assert.equal(r.imported, 2);
  assert.match(r.message, /CHỜ DUYỆT/);
  await assert.rejects(() => importLabel(api, {sku: 'SP-03', text: '  '}));
});

test('catalog drawer mounts the panel and warning text is exported', async () => {
  const src = await readFile(new URL('../../src/web/app/pages/catalog.js', import.meta.url), 'utf8');
  assert.match(src, /doseRulesPanel\(id\)/);
  assert.match(ONLY_APPROVED_WARNING, /Chỉ quy tắc/);
});
