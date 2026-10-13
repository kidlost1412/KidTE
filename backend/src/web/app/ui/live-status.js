import {h} from '../core/dom.js';
import {clock} from '../core/format.js';
import {btn} from './kit.js';
import {icon} from '../core/icons.js';

export const LIVE_REASONS = {
  source_not_started: 'Chưa bật bộ nhận diện', obs_connection_failed: 'Chưa kết nối OBS — kiểm tra WebSocket và mật khẩu',
  obs_not_streaming: 'OBS chưa phát LIVE', obs_select_one_media: 'Có nhiều nguồn media đang chạy — chọn nguồn Shopee',
  obs_media_not_playing: 'Nguồn media chưa phát hoặc không nằm trong cảnh đang lên sóng',
  obs_verify_failed: 'Không xác minh được file video OBS (thiếu ffmpeg hoặc đọc file lỗi) — xem Nhật ký',
  obs_video_not_linked: 'Chưa liên kết file OBS với video trong thư viện',
  obs_video_duration_mismatch: 'Thời lượng file OBS khác video đã liên kết',
  obs_position_invalid: 'OBS trả vị trí hoặc thời lượng không hợp lệ',
  tiktok_studio_not_running: 'Chưa mở TikTok Studio hoặc có nhiều phiên Studio',
  audio_device_error: 'Không thu được nguồn âm đã chọn', audio_silent: 'Nguồn âm đang im lặng',
  no_fingerprint: 'Chưa có vân tay video trong thư viện', no_fingerprint_match: 'Âm thanh chưa khớp video',
  collecting_audio: 'Đang thu đủ âm thanh để nhận diện', confirming_match: 'Đang xác nhận video qua hai lần nghe',
};

// C06: ba trạng thái nguồn video với tuổi đo (đọc từ /api/video/position).
export function videoSourceBadge(p = {}) {
  const sec = v => Number.isFinite(Number(v)) && Number(v) >= 0 ? Math.round(Number(v)) : null;
  const state = String(p.state || ''), verified = sec(p.verification_age_s), age = sec(p.age_s);
  if (state === 'uncertain' || state === 'weak' || state === 'verifying') return {kind: 'uncertain', className: 'chip warn src-uncertain', text: 'Video đã khóa · vị trí chưa chắc chắn' + (verified !== null ? ` · kiểm chứng cách đây ${verified} giây` : '')};
  if (state === 'locked' && p.available !== false) return {kind: 'locked', className: 'chip ok src-locked', text: 'Video đã khóa · vị trí đã kiểm chứng' + (verified !== null ? ` · ${verified} giây trước` : '')};
  if (p.available === false && p.reason === 'stale' || state === 'stale' || state === 'locked') return {kind: 'stale', className: 'chip warn src-stale', text: 'Mất tín hiệu nguồn' + (age !== null ? ` · đo cách đây ${age} giây` : '')};
  return {kind: 'stale', className: 'chip warn src-stale', text: 'Chờ nguồn phát'};
}

export function liveChannelCards(ctx, channels = {}, names = {}) {
  const views = {};
  const grid = h('div', {class: 'live-channel-grid'}, ['tiktok', 'shopee'].map(platform => {
    const label = platform === 'shopee' ? 'Shopee LIVE' : 'TikTok LIVE';
    const source = platform === 'shopee' ? 'OBS Media Source' : 'TikTok Studio';
    const state = h('span',{class:'live-channel-state'});
    const time = h('div',{class:'live-channel-clock'});
    const video = h('div',{class:'live-channel-video'});
    const sourceText = h('span');
    const sync = h('span',{class:'chip'}), ai = h('span',{class:'chip'}), account = h('div',{class:'live-channel-account fs-sm muted'});
    const description = h('p',{class:'live-channel-description'});
    const section = h('section', {class: 'live-channel waiting', 'aria-label': label},
      h('div', {class:'live-channel-stage'},
        h('div', {class:'row between'}, h('div', {class:'live-channel-brand'}, icon('video'), h('strong',{},label)), state),
        time, video, h('div', {class:'live-channel-source'}, icon('activity'), sourceText)),
      h('div',{class:'live-channel-body'},
        h('div',{class:'row wrap gap-2'},sync,ai),account,description,
        h('div',{class:'live-channel-actions'},btn('Quản lý nguồn', {size:'sm',kind:'ghost',icon:'settings',onClick:()=>ctx.navigate('system','live')}),btn('Xem bình luận',{size:'sm',kind:'ghost',icon:'message',onClick:()=>ctx.navigate('inbox','platform',platform)}))));
    views[platform] = {account,section,state,time,video,sourceText,sync,ai,description,source};
    return section;
  }));
  const text = (node,value) => { if (node.textContent !== value) node.textContent = value; };
  grid.update = (next = {}, titles = names) => {
    for (const [platform,v] of Object.entries(views)) {
      const channel = next[platform] || {}, p = channel.position || {}, ext = channel.extension || {}, health = channel.health || {};
      const linked = p.available === true;
      v.section.classList.toggle('linked',linked); v.section.classList.toggle('waiting',!linked);
      text(v.state,ext.connected && ext.live_running ? 'Đang LIVE' : ext.connected ? 'Đã kết nối' : 'Chờ tiện ích');
      text(v.time,linked ? clock(p.position_s * 1000) : '—');
      text(v.video,p.video_id ? titles[p.video_id] || p.video_id : 'Chờ nhận diện video');
      text(v.sourceText,`${v.source} · ${linked ? 'Nguồn riêng đã xác nhận' : 'Chờ tín hiệu'}`);
      const badge = videoSourceBadge(p); text(v.sync,badge.text); v.sync.className = badge.className; v.sync.dataset.kind = badge.kind;
      text(v.ai,ext.connected ? ext.ai_enabled ? ext.ai_ready ? 'AI sẵn sàng' : 'AI đã bật · chờ kết nối' : 'AI đang tắt' : 'AI chờ tiện ích');
      v.ai.className = `chip ${ext.connected && ext.ai_enabled && ext.ai_ready ? 'ok' : 'neutral'}`;
      const host = ext.host || {}, handle = host.unique_id || host.nickname || '';
      text(v.account,platform === 'tiktok' && handle ? `Tài khoản đang live: ${host.unique_id ? '@' + host.unique_id : handle}` : '');
      text(v.description,!linked ? LIVE_REASONS[health.reason] || 'Chưa có vị trí video hợp lệ cho sàn này.' : health.device || (platform === 'shopee' ? 'Đọc vị trí nguồn phương tiện đang phát trong OBS.' : 'Nhận diện âm thanh từ tiến trình TikTok Studio.'));
    }
  };
  grid.update(channels,names);
  return grid;
}
