// Mô hình thuần (không DOM) cho "Xem toàn bộ lời nhắc". Backend vẫn là nơi kiểm tra cuối cùng.
export const PROMPT_VIEW_URL = '/api/learning/prompt-view';
export const BASE_RULES_URL = '/api/learning/base-rules';
export const PERSONA_URL = '/api/learning/persona';
export const NOTES_URL = '/api/system/processors/model-deepseek/notes';
export const TRY_URL = '/api/learning/try';
export const SECTION_ORDER = ['brief', 'rules', 'notes', 'persona'];
export const STYLE_MAX = 1600;

const squash = text => String(text ?? '').replace(/\s+/g, '');

// Các dòng hợp đồng bị xóa khỏi bản đang sửa (backend sẽ tự nối lại) -> UI cảnh báo trước khi lưu.
export function missingProtected(text, protectedLines = []) {
  const s = squash(text);
  return protectedLines.filter(line => !s.includes(squash(line)));
}

// Các phần theo đúng thứ tự gửi, kèm nhãn trạng thái để vẽ.
export function sectionsModel(view = {}) {
  const byId = Object.fromEntries((view.sections || []).map(s => [s.id, s]));
  return SECTION_ORDER.filter(id => byId[id]).map((id, i) => {
    const s = byId[id];
    const badges = [];
    if (!s.editable) badges.push({tone: 'muted', label: 'Chỉ xem'});
    if (id === 'rules') {
      if (s.stale) badges.push({tone: 'warn', label: 'Có bản gốc mới'});
      else if (s.customized) badges.push({tone: 'info', label: 'Bản của bạn'});
      else badges.push({tone: 'ok', label: 'Bản gốc'});
    }
    if (id === 'persona' && s.is_default) badges.push({tone: 'info', label: 'Lưu thành phong cách riêng'});
    return {...s, index: i + 1, badges, text: String(s.text ?? '')};
  });
}

export function noticeModel(view = {}) {
  const n = view.notice;
  if (!n) return {show: false, buttons: []};
  const rows = n.rows || [];
  return {show: true, title: n.title || 'Quy tắc gốc vừa được cập nhật', rows,
    changed: rows.filter(r => r.kind !== 'same').length, newText: n.new_text || '', mineText: n.mine_text || '',
    buttons: [{id: 'use_new', label: 'Dùng bản mới'}, {id: 'keep_mine', label: 'Giữ bản của tôi'}, {id: 'merge', label: 'Gộp tay'}]};
}

export function rulesSaveBody(text, section = {}, personaId = '') {
  const t = String(text ?? '').trim();
  if (t.length < (section.min || 300)) return {error: 'base_rules_too_short'};
  if (t.length > (section.max || 40000)) return {error: 'base_rules_too_long'};
  return {body: {action: 'save', text: t, persona_id: personaId}};
}

export const resolveBody = (action, personaId = '') => ({action, persona_id: personaId});
export const resetBody = (personaId = '') => ({action: 'reset', persona_id: personaId});

// Lưu phần phong cách: người trực thường -> sửa chính họ; mặc định -> tạo phong cách riêng (không đổi giọng dựng sẵn).
export function personaSectionSave(section, text, newName = '') {
  const guide = String(text ?? '').trim();
  if (!guide) return {error: 'persona_proposal_invalid'};
  if (guide.length > (section.max || STYLE_MAX)) return {error: 'persona_style_too_long'};
  if (section.is_default) {
    const name = String(newName || '').trim() || 'Phong cách của tôi';
    return {body: {name: name.slice(0, 80), style_guide: guide}, creates: true};
  }
  return {body: {persona_id: section.persona_id, style_guide: guide}, creates: false};
}

export const previewBody = (comment, personaId) => {
  const c = String(comment ?? '').trim();
  return c.length < 2 ? {error: 'comment_required'} : {body: {comment: c, persona_id: personaId || ''}};
};

export const loadPromptView = (api, personaId = '') =>
  api(`${PROMPT_VIEW_URL}${personaId ? `?persona_id=${encodeURIComponent(personaId)}` : ''}`);

export async function saveRules(api, text, section, personaId = '') {
  const {body, error} = rulesSaveBody(text, section, personaId);
  if (error) throw new Error(error);
  return api(BASE_RULES_URL, {method: 'POST', body});
}
export const resetRules = (api, personaId = '') => api(BASE_RULES_URL, {method: 'POST', body: resetBody(personaId)});
export const resolveRules = (api, action, personaId = '') => api(BASE_RULES_URL, {method: 'POST', body: resolveBody(action, personaId)});

export async function savePersonaSection(api, section, text, newName = '') {
  const {body, error} = personaSectionSave(section, text, newName);
  if (error) throw new Error(error);
  return api(PERSONA_URL, {method: 'POST', body});
}

export async function previewReply(api, comment, personaId) {
  const {body, error} = previewBody(comment, personaId);
  if (error) throw new Error(error);
  return api(TRY_URL, {method: 'POST', body});
}
