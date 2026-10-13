// Mô hình thuần (không DOM) cho "Tạo phong cách bằng AI" và "Ghi chú thêm cho AI".
// api = (url, {method, body}) => Promise. Backend vẫn là nơi kiểm tra cuối cùng.
export const LIMITS = {profile: 1500, minProfile: 12, style: 2400, name: 80, description: 600, notes: 1500};
export const PROPOSE_URL = '/api/learning/persona-propose';
export const PERSONA_URL = '/api/learning/persona';
export const NOTES_URL = '/api/system/processors/model-deepseek/notes';
export const LEGACY_DISMISS_URL = '/api/system/processors/model-deepseek/legacy-dismiss';

// Gợi ý người dùng tả mình (hiện thành chip/placeholder).
export const PROFILE_HINTS = ['giới tính', 'tuổi', 'vùng miền', 'xưng hô', 'tính cách', 'cảm xúc', 'câu cửa miệng', 'cách chốt đơn'];
export const PROFILE_EXAMPLE = 'Nam, 28 tuổi, người miền Tây. Vui, nói thẳng, hay đùa nhẹ, câu cửa miệng “nghe nè”. Chốt đơn dứt khoát, không vòng vo.';
export const PROFILE_AGE_HINT = 'Nhớ ghi TUỔI và giới tính: AI dựa vào đó để chọn xưng hô (mặc định tự xưng “em”, gọi khách “anh/chị”; nam từ khoảng 35 tuổi hoặc bạn muốn xưng khác thì ghi rõ). Chỉ ghi “bà con” khi bạn muốn gọi khách như vậy.';


export const clip = (text, limit) => String(text ?? '').slice(0, limit);

export function validateProfile(text) {
  const t = String(text ?? '').trim();
  if (t.length < LIMITS.minProfile) return 'persona_profile_required';
  return '';
}

export const proposeBody = text => ({profile: clip(String(text ?? '').trim(), LIMITS.profile)});

// Đề xuất của backend -> dữ liệu cho form sửa được.
export function proposalToForm(p = {}) {
  const samples = Array.isArray(p.samples) ? p.samples.map(String) : [];
  const comments = Array.isArray(p.sample_comments) ? p.sample_comments.map(String) : [];
  return {
    name: clip(p.name || 'Phong cách mới', LIMITS.name),
    description: clip(p.description || '', LIMITS.description),
    guide: clip(p.style_guide || '', LIMITS.style),
    samples: comments.length ? comments.map((c, i) => ({comment: c, reply: samples[i] || ''}))
                             : samples.map(r => ({comment: '', reply: r})),
    model: p.model || '',
  };
}

export const styleCounter = text => {
  const used = String(text ?? '').length;
  return {used, left: LIMITS.style - used, over: used > LIMITS.style};
};

// Lưu thành người trực mới. activate = “Chọn người này trực”.
export function personaSaveBody(form, activate = false) {
  const name = String(form.name || '').trim();
  if (!name) return {error: 'persona_name_required'};
  const guide = String(form.guide || '').trim();
  if (!guide) return {error: 'persona_proposal_invalid'};
  if (guide.length > LIMITS.style) return {error: 'persona_style_too_long'};
  return {body: {name: clip(name, LIMITS.name), description: clip(String(form.description || '').trim(), LIMITS.description),
    style_guide: guide, ...(activate ? {activate: true} : {})}};
}

export async function proposePersona(api, text) {
  const error = validateProfile(text);
  if (error) throw new Error(error);
  const r = await api(PROPOSE_URL, {method: 'POST', body: proposeBody(text)});
  return proposalToForm(r.proposal);
}

export async function savePersona(api, form, activate = false) {
  const {body, error} = personaSaveBody(form, activate);
  if (error) throw new Error(error);
  return api(PERSONA_URL, {method: 'POST', body});
}

// ---------------------------------------------------------------- Ghi chú thêm cho AI + lời nhắc kiểu cũ
// Lời nhắc ghi đè TOÀN BỘ không còn được dùng. Nếu máy còn file cũ: báo và cho chuyển thành ghi chú để chủ shop duyệt.
export function legacyModel(stats = {}) {
  const chars = Number(stats.legacy_prompt_chars) || 0;
  const max = Number(stats.notes_max) || LIMITS.notes;
  return {has: chars > 0, chars, fits: chars <= max, text: String(stats.legacy_prompt || ''), max};
}

// Cắt ở ranh giới dòng để chủ shop thấy phần đầu hợp lý; truncated=true thì UI nhắc “đã cắt, hãy xem lại”.
export function migrateToNotes(legacy, max = LIMITS.notes) {
  const text = String(legacy || '').trim();
  if (text.length <= max) return {text, truncated: false};
  const cut = text.slice(0, max);
  const nl = cut.lastIndexOf('\n');
  return {text: (nl > max * 0.5 ? cut.slice(0, nl) : cut).trim(), truncated: true};
}

export function notesSaveBody(text, archiveLegacy = false) {
  const t = String(text ?? '').trim();
  if (t.length > LIMITS.notes) return {error: 'notes_too_long'};
  return {body: {text: t, ...(archiveLegacy ? {archive_legacy: true} : {})}};
}

export async function saveNotes(api, text, archiveLegacy = false) {
  const {body, error} = notesSaveBody(text, archiveLegacy);
  if (error) throw new Error(error);
  return api(NOTES_URL, {method: 'POST', body});
}

export const dismissLegacy = api => api(LEGACY_DISMISS_URL, {method: 'POST', body: {}});
