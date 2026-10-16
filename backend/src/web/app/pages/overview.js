import {aiStatus} from '../core/ai-status.js';
import {h, clear, disposer} from '../core/dom.js';
import {icon} from '../core/icons.js';
import {request} from '../core/api.js';
import {number, relative, clock} from '../core/format.js';
import {btn, chip, empty, pageHead, personAvatar, bag, productAvatar, skeletonRows} from '../ui/kit.js';
import {CONVERSATION_STATUS, RESOURCE_NAME, lookup} from '../labels.js';
import {liveChannelCards} from '../ui/live-status.js';
import {liveWizard} from './live-wizard.js';

function kpi(label, value, hint, iconName, onClick, tone) {
  return h(onClick ? 'button' : 'div', {class: 'kpi' + (onClick ? ' link' : ''), type: onClick ? 'button' : null, onClick},
    h('div', {class: 'kpi-top'}, h('span', {}, label), icon(iconName)),
    h('div', {class: 'kpi-value', style: tone ? {color: `var(--${tone}-text)`} : null}, value),
    hint ? h('div', {class: 'kpi-hint'}, hint) : null);
}

function checkItem(state, title, text, action) {
  return h('div', {class: 'check-item ' + state},
    h('span', {class: 'ck'}, icon(state === 'ok' ? 'check' : state === 'danger' ? 'x' : 'alert')),
    h('div', {style: {minWidth: '0'}}, h('strong', {}, title), text ? h('small', {}, text) : null),
    action || null);
}

const ACTIVITY_PAGE = {
  videos: ['video', 'studio'], 'video-jobs': ['video', 'review'], profiles: ['catalog', 'profiles'], products: ['catalog', 'products'],
  conversations: ['inbox', 'all'], 'raw-events': ['inbox', 'all'], 'product-images': ['catalog', 'products'],
};

export function queuePanelModel(q) {
  if (!q || typeof q !== 'object') return null;
  const n = v => Number.isFinite(+v) ? +v : 0;
  return {pending: n(q.pending), oldest: n(q.oldest_age_s), expired: n(q.expired), unknown: n(q.unknown), mode: q.queue_mode === true,
    warn: n(q.unknown) > 0};
}

function queuePanel(q) {
  const m = queuePanelModel(q);
  if (!m) return null;
  const cell = (label, value, tone) => h('div', {class: 'queue-cell'}, h('small', {}, label), h('strong', {style: tone ? {color: `var(--${tone}-text)`} : null}, number(value)));
  return h('section', {class: 'card queue-panel'},
    h('header', {class: 'card-head'}, h('div', {class: 'grow'}, h('h2', {}, 'Hàng chờ AI'), h('div', {class: 'sub'}, '24 giờ gần nhất')), chip(m.mode ? 'Chế độ queue: bật' : 'Chế độ queue: tắt', m.mode ? 'ok' : 'neutral')),
    h('div', {class: 'queue-cells'}, cell('Đang chờ', m.pending), cell('Chờ lâu nhất (giây)', m.oldest), cell('Hết hạn', m.expired),
      cell('Chưa rõ — cần đối soát', m.unknown, m.warn ? 'danger' : null)));
}

export default {
  mount(ctx) {
    const d = disposer();
    const wizard = liveWizard(ctx);
    const heroSlot = h('div', {class: 'live-overview'});
    const kpiSlot = h('div', {class: 'kpis'});
    const queueSlot = h('div');
    const pendingSlot = h('div', {}, skeletonRows(4, 50));
    const activitySlot = h('div');
    const page = h('div', {class: 'page'},
      pageHead('Trung tâm LIVE', 'Hai nguồn phát độc lập. Một nơi quản lý khách hàng, kiến thức và vận hành.',
        [btn('Mở hộp thư', {kind: 'primary', icon: 'message', onClick: () => ctx.navigate('inbox')})]),
      wizard.el, heroSlot, kpiSlot, queueSlot,
      h('div', {class: 'overview-grid'},
        h('section', {class: 'card'}, h('header', {class: 'card-head'}, h('div', {class: 'grow'}, h('h2', {}, 'Việc cần làm trong Hộp thư'), h('div', {class: 'sub'}, 'Khách hỏi mà chưa ai trả lời và chưa được dạy AI — mới nhất trước')),
          btn('Mở hàng “Cần xử lý”', {kind: 'ghost', size: 'sm', icon: 'arrowRight', onClick: () => ctx.navigate('inbox', 'todo')})), pendingSlot),
        h('section', {class: 'card'}, h('header', {class: 'card-head'}, h('div', {class: 'grow'}, h('h2', {}, 'Thay đổi gần đây'), h('div', {class: 'sub'}, 'Những gì đã được sửa, duyệt hoặc áp dụng'))), activitySlot)));
    clear(ctx.shell.main, page);

    let settings = null;
    let videoNames = {};
    const channelGrid = liveChannelCards(ctx);
    const servicesSlot = h('div');
    let lastKey = '';
    request('/api/settings').then(s => { settings = s; render(ctx.store.get().dashboard, true); }).catch(() => {});
    request('/api/workspace/videos?limit=100').then(r => { (r.items || []).forEach(v => { videoNames[v.id] = v.title; }); render(ctx.store.get().dashboard, true); }).catch(() => {});

    function render(dash, force = false) {
      if (!dash) { clear(heroSlot, h('div', {class: 'skeleton', style: {height: '188px', borderRadius: '20px'}}), h('div', {class: 'skeleton', style: {height: '188px', borderRadius: '20px'}})); return; }
      wizard.update(dash, videoNames);
      if (!channelGrid.isConnected) clear(heroSlot,h('div',{class:'col gap-4'},channelGrid,servicesSlot));
      channelGrid.update(dash.live_channels,videoNames);
      // Source positions change on every poll. Patch only their text; retain
      // action elements and focus while the rest of the dashboard is unchanged.
      const key = JSON.stringify([dash.live_channels?.shopee?.catalog?.products_count, dash.captcha && [dash.captcha.running, dash.captcha.configured, dash.captcha.key_rejected], dash.session && [dash.session.todo, dash.session.review, dash.session.ai_replied], dash.catalog, dash.backup, dash.video_review, dash.learning, dash.ai_queue, dash.privacy, dash.recent_activity, dash.readiness && dash.readiness.summary, dash.live_control, dash.ai && dash.ai.deepseek_available, settings && settings.selected_audio_device, Object.keys(videoNames).length]);
      if (!force && key === lastKey) return;
      lastKey = key;
      const ext = dash.extension || {};
      const connected = Boolean(ext.connected);
      const live = connected && ext.live_running;
      const video = dash.video || {};
      const syncing = video.video_id && ['watching', 'detected', 'running', 'tracking'].includes(video.state);
      const session = dash.session || {};
      const catalog = dash.catalog || {};
      const plugins = (dash.plugins && dash.plugins.plugins) || [];
      const syncPlugin = plugins.find(p => p.id === 'dong-bo-video');
      const learning = dash.learning || null;

      clear(servicesSlot,
        h('section', {class: 'hero-checklist card'}, h('h3', {class:'shared-services-title'}, 'Dữ liệu và dịch vụ dùng chung'),
          checkItem(aiStatus(dash.ai).kind, 'DeepSeek', aiStatus(dash.ai).kind === 'ok' ? 'Sẵn sàng cho cả hai sàn; bật trả lời trong từng tiện ích' : aiStatus(dash.ai).checking ? aiStatus(dash.ai).text : 'Chưa kết nối AI', btn('Cài đặt', {size:'sm', onClick:()=>ctx.navigate('system','setup')})),
          checkItem(catalog.total ? 'ok' : 'warn', 'Giỏ TikTok', `${number(catalog.total)} sản phẩm · ${number(catalog.mapped)} có hồ sơ`, btn('Xem giỏ', {size:'sm', onClick:()=>ctx.navigate('catalog')})),
          checkItem(dash.live_channels?.shopee?.catalog?.products_count ? 'ok' : 'warn', 'Giỏ Shopee', `${number(dash.live_channels?.shopee?.catalog?.products_count || 0)} sản phẩm · ánh xạ theo kịch bản Shopee`, btn('Nguồn LIVE', {size:'sm', onClick:()=>ctx.navigate('system','live')})),
          checkItem(dash.backup && !dash.backup.overdue ? 'ok' : 'warn', 'Sao lưu dùng chung', dash.backup ? `Gần nhất ${relative(dash.backup.latest)}` : 'Chưa có bản sao lưu', btn('Xem', {size:'sm',onClick:()=>ctx.navigate('system','backups')})),
          privacyWarning(dash.privacy) ? checkItem('danger', 'Bộ nhớ khách', privacyWarning(dash.privacy)) : null));

      clear(queueSlot, queuePanel(dash.ai_queue));

      const todo = session.todo ?? session.unanswered ?? 0;
      clear(kpiSlot,
        kpi('Cần xử lý', number(todo), 'Khách hỏi, chưa ai trả lời hay dạy AI', 'alert', () => ctx.navigate('inbox', 'todo'), todo ? 'danger' : null),
        kpi('Duyệt câu AI', number(session.review || 0), `AI đã gửi ${number(session.ai_replied || 0)} câu — xem, nắn lại cho đúng giọng`, 'sparkles', () => ctx.navigate('inbox', 'review')),
        kpi('Trí nhớ AI', number((learning && learning.active) || 0), learning && learning.suggested ? `${learning.suggested} điều AI đề xuất chờ bạn duyệt` : 'điều AI đã được dạy và đang dùng', 'layers', () => ctx.navigate('brain'), learning && learning.suggested ? 'warn' : null),
        kpi('Giỏ TikTok có hồ sơ', `${number(catalog.mapped || 0)}/${number(catalog.total || 0)}`, catalog.unmapped ? `${catalog.unmapped} SP AI chưa được phép tư vấn` : 'AI tư vấn được mọi SP', 'box', () => ctx.navigate('catalog', catalog.unmapped ? 'mapping' : 'products'), catalog.unmapped ? 'warn' : null));

      const rows = dash.recent_activity || [];
      clear(activitySlot, rows.length ? rows.map(row => {
        const target = ACTIVITY_PAGE[row.resource];
        const what = RESOURCE_NAME[row.resource] || row.resource;
        const name = row.resource === 'videos' || row.resource === 'video-jobs' ? (videoNames[row.record_id] || '') : '';
        return h('div', {class: 'activity'},
          h('span', {class: 'a-icon'}, icon(row.label.includes('Thùng rác') ? 'trash' : row.label.includes('khôi phục') ? 'undo' : 'edit')),
          h('div', {style: {minWidth: '0'}}, h('strong', {class: 'fs-sm'}, `${row.label} · ${what}${name ? ' “' + name + '”' : ''}`), h('div', {class: 'fs-xs muted truncate'}, row.description)),
          h('div', {class: 'col', style: {alignItems: 'flex-end', gap: '4px'}}, h('span', {class: 'fs-xs muted nowrap'}, relative(row.when)),
            target ? btn('Mở', {size: 'sm', kind: 'ghost', onClick: () => ctx.navigate(target[0], target[1], ['studio', 'review', 'all'].includes(target[1]) ? row.record_id : '')}) : null));
      }) : empty('Chưa có thay đổi nào', 'Khi bạn sửa hồ sơ, duyệt câu trả lời hay áp dụng lịch ghim, lịch sử sẽ hiện ở đây.', {iconName: 'clock'}));
    }

    async function loadPending() {
      try {
        const out = await request('/api/workspace/conversations?view=summary&limit=200');
        const handled = x => x.taught || (x.response_quality && x.response_quality !== 'unreviewed');
        const items = (out.items || []).filter(r => ['unanswered', 'waiting', 'ai_failed'].includes(r.conversation_status) && !handled(r)).slice(0, 6);
        clear(pendingSlot, items.length ? h('div', {class: 'list'}, items.map(row => {
          const x = row.effective || row;
          const st = lookup(CONVERSATION_STATUS, x.conversation_status);
          return h('button', {class: 'list-row', type: 'button', onClick: () => ctx.navigate('inbox', 'todo', row.id)},
            personAvatar(x.nickname || x.unique_id, x.user_id || x.unique_id),
            h('div', {class: 'grow'}, h('div', {class: 'row'}, h('span', {class: 'title truncate'}, x.nickname || x.unique_id || 'Khách'), h('span', {class: 'sub nowrap', style: {marginLeft: 'auto'}}, relative(x.ts))),
              h('div', {class: 'fs-sm clamp-2', style: {marginTop: '2px'}}, x.original_text || x.text),
              h('div', {class: 'row', style: {marginTop: '4px'}}, chip(st.short || st.label, st.tone), x.no_reply_reason ? h('span', {class: 'sub truncate'}, x.no_reply_reason) : null)));
        })) : empty('Không còn việc cần làm', 'Mọi câu hỏi của khách đều đã được trả lời hoặc dạy AI.', {iconName: 'checkCircle'}));
      } catch (_) { clear(pendingSlot, empty('Chưa tải được bình luận', 'Thử lại sau vài giây.')); }
    }

    d.add(ctx.store.subscribe(state => render(state.dashboard)));
    render(ctx.store.get().dashboard, true);
    loadPending();
    d.interval(loadPending, 20000);
    return {unmount: () => d.dispose()};
  },
};

export function privacyWarning(privacy) {
  if (!privacy || privacy.memory_available !== false) return '';
  return privacy.warning || 'Bộ nhớ khách tạm tắt: khoá quyền riêng tư không đọc được.';
}
