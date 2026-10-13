import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {LIMITS, PROPOSE_URL, PERSONA_URL, NOTES_URL, LEGACY_DISMISS_URL, PROFILE_HINTS, validateProfile, proposeBody, proposalToForm,
  personaSaveBody, styleCounter, proposePersona, savePersona, legacyModel, migrateToNotes, notesSaveBody, saveNotes, dismissLegacy,
} from '../../src/web/app/pages/persona-model.js';

const PROPOSAL = {name: 'Anh Ba miền Tây', description: 'Nam, 35 tuổi', style_guide: '- Xưng hô: anh/tui, gọi khách bà con\n- Cách chốt: đặt liền anh giữ hàng',
  samples: ['Khỏe không bà con!', 'Pha 20ml nghe bà con.', 'Để anh coi giá nha.'], sample_comments: ['chào shop', 'chai này pha bao nhiêu vậy', 'giá sao bạn ơi'], model: 'm'};

test('hints cover everything the owner asked the host to describe', () => {
  for (const word of ['giới tính', 'tuổi', 'vùng miền', 'xưng hô', 'tính cách', 'cảm xúc', 'câu cửa miệng', 'cách chốt đơn']) assert.ok(PROFILE_HINTS.includes(word), word);
});

test('profile must be long enough and is clipped to the backend limit', () => {
  assert.equal(validateProfile('ngắn'), 'persona_profile_required');
  assert.equal(validateProfile('   '), 'persona_profile_required');
  assert.equal(validateProfile('Nam 35 tuổi miền Tây, xưng anh'), '');
  assert.equal(proposeBody('x'.repeat(5000)).profile.length, LIMITS.profile);
  assert.equal(proposeBody('  Nam miền Tây  ').profile, 'Nam miền Tây');
});

test('proposal turns into an editable form with a sample per typical comment', () => {
  const form = proposalToForm(PROPOSAL);
  assert.equal(form.name, 'Anh Ba miền Tây');
  assert.match(form.guide, /bà con/);
  assert.deepEqual(form.samples.map(x => x.comment), PROPOSAL.sample_comments);
  assert.equal(form.samples[1].reply, 'Pha 20ml nghe bà con.');
  assert.deepEqual(proposalToForm({}).samples, []);
  assert.equal(proposalToForm({}).name, 'Phong cách mới');
  assert.equal(proposalToForm({style_guide: 'x'.repeat(3000)}).guide.length, LIMITS.style);
});

test('style counter flags text over the 1600 character limit', () => {
  assert.deepEqual(styleCounter('abc'), {used: 3, left: LIMITS.style - 3, over: false});
  assert.equal(styleCounter('x'.repeat(LIMITS.style + 1)).over, true);
});

test('save body: name and guide required, activate only when asked, limit enforced', () => {
  const form = proposalToForm(PROPOSAL);
  const plain = personaSaveBody(form);
  assert.deepEqual(plain.body, {name: 'Anh Ba miền Tây', description: 'Nam, 35 tuổi', style_guide: PROPOSAL.style_guide});
  assert.equal(personaSaveBody(form, true).body.activate, true);
  assert.equal(personaSaveBody({...form, name: ' '}).error, 'persona_name_required');
  assert.equal(personaSaveBody({...form, guide: ' '}).error, 'persona_proposal_invalid');
  assert.equal(personaSaveBody({...form, guide: 'x'.repeat(LIMITS.style + 1)}).error, 'persona_style_too_long');
});

test('propose and save call the learning endpoints (nothing is saved by propose)', async () => {
  const calls = [];
  const api = async (url, opts) => { calls.push([url, opts]); return url === PROPOSE_URL ? {ok: true, proposal: PROPOSAL} : {ok: true}; };
  await assert.rejects(() => proposePersona(api, 'ngắn'), /persona_profile_required/);
  assert.equal(calls.length, 0);
  const form = await proposePersona(api, 'Nam 35 tuổi miền Tây, xưng anh, gọi khách bà con');
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], '/api/learning/persona-propose');
  assert.equal(calls[0][1].method, 'POST');
  await savePersona(api, form, true);
  assert.equal(calls[1][0], PERSONA_URL);
  assert.equal(calls[1][1].body.activate, true);
  await assert.rejects(() => savePersona(api, {...form, name: ''}), /persona_name_required/);
  assert.equal(calls.length, 2);
});

test('legacy override is reported, never dropped silently', () => {
  assert.equal(legacyModel({}).has, false);
  const m = legacyModel({legacy_prompt: 'Bạn là cô gái…', legacy_prompt_chars: 5000, notes_max: 1500});
  assert.equal(m.has, true);
  assert.equal(m.fits, false);
  assert.equal(m.text, 'Bạn là cô gái…');
  assert.equal(legacyModel({legacy_prompt_chars: 200, legacy_prompt: 'x', notes_max: 1500}).fits, true);
});

test('migrating a long legacy prompt cuts at a line and flags truncation for review', () => {
  const short = migrateToNotes('Gọi khách là bà con', 1500);
  assert.deepEqual(short, {text: 'Gọi khách là bà con', truncated: false});
  const long = Array.from({length: 200}, (_, i) => `Dòng số ${i} của lời nhắc cũ`).join('\n');
  const cut = migrateToNotes(long, 300);
  assert.equal(cut.truncated, true);
  assert.ok(cut.text.length <= 300);
  assert.ok(!cut.text.endsWith('\n') && long.startsWith(cut.text));
  assert.match(cut.text.split('\n').at(-1), /^Dòng số \d+ của lời nhắc cũ$/);
});

test('notes save: limit, archive flag and endpoints', async () => {
  assert.equal(notesSaveBody('x'.repeat(LIMITS.notes + 1)).error, 'notes_too_long');
  assert.deepEqual(notesSaveBody('  ghi chú  ').body, {text: 'ghi chú'});
  assert.deepEqual(notesSaveBody('ghi chú', true).body, {text: 'ghi chú', archive_legacy: true});
  const calls = [];
  const api = async (url, opts) => { calls.push([url, opts]); return {ok: true}; };
  await saveNotes(api, 'Shop giao tận vườn', true);
  await dismissLegacy(api);
  assert.equal(calls[0][0], NOTES_URL);
  assert.equal(calls[0][1].body.archive_legacy, true);
  assert.equal(calls[1][0], LEGACY_DISMISS_URL);
  await assert.rejects(() => saveNotes(api, 'x'.repeat(LIMITS.notes + 1)), /notes_too_long/);
  assert.equal(calls.length, 2);
});

test('automation page no longer has the full-prompt editor and brain page has the AI creator', async () => {
  const automation = await readFile(new URL('../../src/web/app/pages/automation.js', import.meta.url), 'utf8');
  assert.ok(!automation.includes('Lời nhắc hệ thống (cách AI được dặn trả lời)'));
  assert.ok(!automation.includes('/prompt'));
  assert.ok(automation.includes('Ghi chú thêm cho AI') && automation.includes('Chuyển thành ghi chú'));
  const brain = await readFile(new URL('../../src/web/app/pages/brain.js', import.meta.url), 'utf8');
  assert.ok(brain.includes('Tạo phong cách bằng AI') && brain.includes('Lưu và chọn người này trực'));
  const api = await readFile(new URL('../../src/web/app/core/api.js', import.meta.url), 'utf8');
  for (const code of ['persona_proposal_failed', 'persona_profile_required', 'model_not_ready', 'notes_too_long', 'persona_style_too_long']) assert.ok(api.includes(code + ':'), code);
});
