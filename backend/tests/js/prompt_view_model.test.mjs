import test from 'node:test';
import assert from 'node:assert/strict';
import {sectionsModel, noticeModel, missingProtected, rulesSaveBody, personaSectionSave, previewBody, resolveBody,
  loadPromptView, savePersonaSection, resolveRules, STYLE_MAX} from '../../src/web/app/pages/prompt-view-model.js';

const view = (over = {}) => ({sections: [
  {id: 'persona', title: '4)', text: 'giọng', editable: true, is_default: true, max: 1600, persona_id: 'default'},
  {id: 'notes', title: '3)', text: '', editable: true, max: 1500},
  {id: 'rules', title: '2)', text: 'luật', editable: true, customized: false, stale: false, min: 300, max: 40000},
  {id: 'brief', title: '1)', text: 'brief', editable: false}], notice: null, ...over});

test('sections come in send order with state badges', () => {
  const m = sectionsModel(view());
  assert.deepEqual(m.map(s => s.id), ['brief', 'rules', 'notes', 'persona']);
  assert.deepEqual(m.map(s => s.index), [1, 2, 3, 4]);
  assert.equal(m[0].badges[0].label, 'Chỉ xem');
  assert.equal(m[1].badges[0].label, 'Bản gốc');
  assert.ok(m[3].badges.some(b => /riêng/.test(b.label)));
  const custom = sectionsModel(view({sections: [{id: 'rules', text: 'x', editable: true, customized: true, stale: true}]}));
  assert.equal(custom[0].badges[0].label, 'Có bản gốc mới');
});

test('notice offers the three choices only when rules are stale', () => {
  assert.deepEqual(noticeModel(view()), {show: false, buttons: []});
  const n = noticeModel(view({notice: {rows: [{kind: 'same'}, {kind: 'changed'}, {kind: 'added'}], new_text: 'n', mine_text: 'm'}}));
  assert.equal(n.show, true);
  assert.equal(n.changed, 2);
  assert.deepEqual(n.buttons.map(b => b.label), ['Dùng bản mới', 'Giữ bản của tôi', 'Gộp tay']);
  assert.equal(n.title, 'Quy tắc gốc vừa được cập nhật');
});

test('protected-line warning detects deleted contract lines ignoring whitespace', () => {
  const lines = ['{"action":"reply","reply":"..."}', '- Phần reply phải nằm trong giới hạn'];
  assert.deepEqual(missingProtected('{ "action":"reply", "reply":"..." }\n- Phần reply phải nằm trong giới hạn', lines), []);
  assert.deepEqual(missingProtected('không còn gì', lines), lines);
});

test('rules save validates length and carries persona id', () => {
  const s = {min: 300, max: 40000};
  assert.equal(rulesSaveBody('ngắn', s).error, 'base_rules_too_short');
  assert.equal(rulesSaveBody('x'.repeat(40001), s).error, 'base_rules_too_long');
  assert.deepEqual(rulesSaveBody(' ' + 'x'.repeat(300) + ' ', s, 'p1').body, {action: 'save', text: 'x'.repeat(300), persona_id: 'p1'});
  assert.deepEqual(resolveBody('keep_mine', 'p1'), {action: 'keep_mine', persona_id: 'p1'});
});

test('persona section: default creates its own style, others edit in place', () => {
  assert.equal(personaSectionSave({is_default: true}, '  ').error, 'persona_proposal_invalid');
  assert.equal(personaSectionSave({is_default: true}, 'x'.repeat(STYLE_MAX + 1)).error, 'persona_style_too_long');
  const d = personaSectionSave({is_default: true, persona_id: 'default'}, '- Giọng', '');
  assert.deepEqual(d, {body: {name: 'Phong cách của tôi', style_guide: '- Giọng'}, creates: true});
  const p = personaSectionSave({is_default: false, persona_id: 'p9'}, '- Xưng anh');
  assert.deepEqual(p, {body: {persona_id: 'p9', style_guide: '- Xưng anh'}, creates: false});
});

test('preview goes through the Thử AI body with the persona', () => {
  assert.equal(previewBody(' ').error, 'comment_required');
  assert.deepEqual(previewBody(' chai này pha sao ', 'p9').body, {comment: 'chai này pha sao', persona_id: 'p9'});
});

test('api helpers hit the right urls', async () => {
  const calls = [];
  const api = async (url, opt) => { calls.push([url, opt && opt.body]); return {ok: true}; };
  await loadPromptView(api, 'a b');
  await resolveRules(api, 'use_new', 'p1');
  await savePersonaSection(api, {is_default: false, persona_id: 'p1'}, '- x');
  assert.equal(calls[0][0], '/api/learning/prompt-view?persona_id=a%20b');
  assert.deepEqual(calls[1], ['/api/learning/base-rules', {action: 'use_new', persona_id: 'p1'}]);
  assert.equal(calls[2][0], '/api/learning/persona');
});
