import {h, clear, disposer} from '../core/dom.js';
import {request, friendlyError} from '../core/api.js';
import {btn, card, field, input, notice, pageHead} from '../ui/kit.js';
import {toast} from '../ui/overlays.js';
// Mở trang ngoài bằng trình duyệt mặc định qua allowlist OPEN_TARGETS phía server.
function helperLink(label, target) {
  return btn(label, {kind: 'ghost', size: 'sm', icon: 'arrowRight', onClick: async () => {
    try { await request('/api/system/open', {method: 'POST', body: {target}}); }
    catch (error) { toast(friendlyError(error), 'danger'); }
  }});
}
import {healthModel, selftestRows} from './captcha-model.js';

const TONE_VAR = {ok: 'var(--ok, #1a7f37)', info: 'var(--info, #0969da)', warn: 'var(--warn, #9a6700)', danger: 'var(--danger, #cf222e)'};
function healthCard(m) {
  return h('div', {class: 'col gap-2', style: {padding: '14px', borderRadius: '10px', border: '2px solid ' + (TONE_VAR[m.tone] || 'var(--border)')}},
    h('div', {style: {fontSize: '1.4rem', fontWeight: '700', color: TONE_VAR[m.tone]}}, m.label),
    m.sub ? h('div', {class: 'fs-sm muted'}, m.sub) : null,
    m.result ? h('div', {class: 'fs-sm'}, 'Lần giải gần nhất: ' + m.result) : null,
    m.stats.length ? h('div', {style: {display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(160px,1fr))', gap: '10px'}},
      m.stats.map(x => h('div', {}, h('div', {class: 'fs-sm muted'}, x.name), h('div', {}, x.value)))) : null);
}

export default {
  mount(ctx) {
    const d = disposer();
    const status = h('div', {class: 'col gap-3'});
    const selftestOut = h('div', {class: 'col gap-2'});
    const history = h('div', {class: 'col gap-3'});
    const monitorHistory = h('div', {class: 'col gap-3'});
    const keyStatus = h('p', {class: 'fs-sm muted'});
    const telegramStatus = h('p', {class: 'fs-sm muted'});
    const key = input({type: 'password', autocomplete: 'off', placeholder: 'SadCaptcha API key'});
    const botToken = input({type: 'password', autocomplete: 'off', placeholder: 'Telegram bot token'});
    const chatId = input({placeholder: 'Chat ID dạng số'});
    const save = btn('Lưu key', {kind: 'primary', onClick: async () => {
      if (!key.value.trim()) { toast('Nhập SadCaptcha API key trước.', 'danger'); return; }
      try {
        const result = await request('/api/captcha/settings', {
          method: 'POST', body: {api_key: key.value.trim()},
        });
        key.value = '';
        toast(result.restart_required ? 'Đã lưu key. Khởi động lại solver để dùng key mới.' :
          result.start_requested ? 'Đã lưu key và yêu cầu khởi chạy solver.' :
          'Đã lưu key. Chưa khởi chạy được solver: ' + friendlyError({code: result.start_error}),
          result.start_error ? 'danger' : 'ok');
        refresh();
      } catch (error) { toast(friendlyError(error), 'danger'); }
    }});
    const actions = h('div', {class: 'row gap-3'},
      btn('Khởi chạy solver', {onClick: async () => {
        try { await request('/api/captcha/start', {method: 'POST', body: {}}); toast('Đã yêu cầu khởi chạy.', 'ok'); refresh(); }
        catch (error) { toast(friendlyError(error), 'danger'); }
      }}),
      btn('Kiểm tra thiết lập', {kind: 'ghost', onClick: async () => {
        clear(selftestOut, h('p', {class: 'fs-sm muted'}, 'Đang kiểm tra...'));
        let result;
        try { result = await request('/api/captcha/selftest', {method: 'POST', body: {}, timeoutMs: 60000}); }
        catch (error) { result = (error && error.payload) || {error: 'captcha_selftest_failed'}; if (!result.checks) result = {...result, checks: []}; }
        clear(selftestOut, selftestRows(result).map(row => h('div', {class: 'fs-sm'},
          h('strong', {}, (row.ok ? 'Đạt' : 'Không đạt') + ' · ' + row.name),
          row.detail ? ' — ' + row.detail : '', row.ok || !row.hint ? null : h('div', {class: 'muted'}, row.hint))));
      }}),
      btn('Dừng solver', {kind: 'ghost', onClick: async () => {
        try { await request('/api/captcha/stop', {method: 'POST', body: {}}); refresh(); }
        catch (error) { toast(friendlyError(error), 'danger'); }
      }}));
    clear(ctx.shell.main, h('div', {class: 'page'},
      pageHead('Giải CAPTCHA', 'Solver chạy trong tiến trình riêng; Kid TE chỉ khởi chạy và đọc nhật ký.'),
      h('div', {class: 'captcha-layout'},
        card('Trạng thái', h('div', {class: 'card-body col gap-3'}, status, actions, selftestOut)),
        h('div', {class: 'captcha-grid'},
          card('SadCaptcha', h('div', {class: 'card-body col gap-3'},
            keyStatus,
            field('API key', key, {hint: 'Key mã hóa bằng Windows DPAPI trên máy này; Kid TE không hiển thị lại.'}),
            helperLink('Lấy API key', 'sadcaptcha_dashboard')),
          {footer: save}),
          card('Cảnh báo Telegram và video', h('div', {class: 'card-body col gap-3'},
            telegramStatus,
            h('p', {class: 'fs-sm muted'}, 'Khi solver ghi nhận CAPTCHA mới, tiến trình riêng quay vùng cửa sổ TikTok Studio trong 30 giây. Video chờ gửi được giữ tối đa 7 ngày; chỉ xoá sau khi Telegram xác nhận nhận thành công. Kho quay giới hạn 5 GB; khi đầy sẽ dừng quay và solver vẫn tiếp tục.'),
            field('Mã bot Telegram', botToken, {hint: 'Để trống nếu chỉ sửa Chat ID đã lưu.'}),
            helperLink('Tạo bot (BotFather)', 'telegram_botfather'),
            field('Chat ID', chatId),
            h('div', {class: 'col gap-2'},
              helperLink('Lấy chat ID (@userinfobot)', 'telegram_userinfobot'),
              h('div', {class: 'fs-sm muted'}, 'Nhắn /start cho bot này, nó trả về ID của bạn.'))),
          {footer: [
            btn('Gửi tin thử', {kind: 'ghost', onClick: async () => {
              try {
                await request('/api/captcha/telegram/test', {method: 'POST', body: {}});
                toast('Telegram đã nhận tin nhắn thử.', 'ok');
              } catch (error) { toast(friendlyError(error), 'danger'); }
            }}),
            btn('Lưu Telegram', {onClick: async () => {
              try {
                const result = await request('/api/captcha/telegram', {
                  method: 'POST', body: {bot_token: botToken.value.trim(), chat_id: chatId.value.trim()},
                });
                botToken.value = '';
                toast(result.monitor_error ?
                  'Đã lưu Telegram, nhưng giám sát chưa chạy: ' + friendlyError({code: result.monitor_error}) :
                  result.monitor_start_requested ? 'Đã lưu và yêu cầu bật giám sát Telegram.' : 'Đã lưu Telegram.',
                result.monitor_error ? 'danger' : 'ok');
                refresh();
              } catch (error) { toast(friendlyError(error), 'danger'); }
            }}),
          ]})),
        h('div', {class: 'captcha-grid'},
          card('Nhật ký solver', h('div', {class: 'card-body'}, history)),
          card('Giám sát Telegram và quay màn hình',
            h('div', {class: 'card-body'}, monitorHistory))))));

    let active = true;
    let refreshing = false;
    async function refresh() {
      if (!active || refreshing) return;
      refreshing = true;
      try {
        const state = await request('/api/captcha');
        if (!active) return;
        keyStatus.textContent = state.configured ? 'Key đã lưu và được mã hóa. Để thay key, nhập key mới bên dưới.' : 'Chưa lưu SadCaptcha API key.';
        telegramStatus.textContent = state.telegram_configured ?
          `Mã bot Telegram đã lưu · Chat ID ${state.telegram_chat_hint || 'đã lưu'}.` : 'Chưa lưu mã bot Telegram và Chat ID.';
        const model = healthModel(state);
        clear(status,
          healthCard(model),
          !state.available ? notice('warn', state.package_error ? 'Gói CAPTCHA cần được cài lại' : 'Gói CAPTCHA chưa được cài',
            h('div', {class: 'col gap-2'},
              h('div', {}, state.package_error ? friendlyError({code: state.package_error}) :
                'Vào Cài đặt → Cập nhật, chọn gói CAPTCHA rồi cài để bật solver, giám sát Telegram và quay video.'),
              btn('Mở Cài đặt → Cập nhật', {kind: 'primary', onClick: () => ctx.navigate('system', 'updates')}))) : null,
          notice(state.running ? 'ok' : 'warn', state.running ? 'Tiến trình đang chạy' : 'Tiến trình chưa chạy',
          state.running ? `PID ${state.pid} · ${state.admin ? 'quyền Administrator' : 'đang xác minh quyền Admin'}` :
              state.key_rejected ? 'SadCaptcha đã từ chối key; cần lưu key mới.' :
              !state.configured ? 'Chưa có API key.' :
              !state.available ? 'Cần cài gói CAPTCHA để khởi chạy solver.' :
              state.error || 'Đã sẵn sàng để khởi chạy.'),
          state.external_solver_running ? notice('warn', 'Có solver khác đang chạy',
            `PID ${state.external_pid}. Kid TE sẽ không tạo tiến trình thứ hai.`) : null,
          state.monitor_pid ? notice('info', 'Giám sát Telegram đang chạy',
            `PID ${state.monitor_pid}. Tin và video sẽ được gửi khi solver ghi nhận CAPTCHA.`) : null);
        if (state.telegram_error) {
          status.append(notice('danger', 'Cấu hình Telegram chưa dùng được', friendlyError({code: state.telegram_error})));
        } else if (state.telegram_configured && !state.monitor_pid) {
          status.append(notice('warn', 'Giám sát Telegram chưa chạy',
            state.monitor_error ? friendlyError({code: state.monitor_error}) :
            'Nhấn Khởi chạy solver để bật giám sát cho phiên đang chạy.'));
        }
        status.append(notice('info', 'Key chỉ dùng trên máy này',
          'Key SadCaptcha và Telegram được mã hóa bằng Windows DPAPI theo từng máy và từng tài khoản Windows. Khi chuyển sang máy mới hoặc tài khoản Windows khác, hãy nhập lại.'));
        const uploads = state.video_uploads || {};
        if (uploads.recording_paused) status.append(notice('warn', 'Kho video CAPTCHA gần đầy',
          'Đã dừng quay mới để giữ giới hạn 5 GB. Video đang chờ vẫn được gửi lại; solver tiếp tục giải CAPTCHA.'));
        if (uploads.pending || uploads.sending) status.append(notice('info', 'Video đang chờ Telegram',
          `${uploads.pending || 0} video chờ · ${uploads.sending || 0} đang gửi. ${state.running ? 'Tự thử lại khi tới hạn.' : 'Khởi chạy solver để tiếp tục hàng chờ gửi.'}`));
        if (state.running && state.log_age_seconds != null && state.log_age_seconds > 120) {
          status.append(notice('warn', 'Solver chưa ghi log mới',
            `Nhật ký native đã im lặng ${state.log_age_seconds} giây. Kiểm tra cửa sổ TikTok và tiến trình solver.`));
        }
        clear(history, (state.events || []).length
          ? state.events.slice().reverse().map(event =>
            h('div', {class: 'fs-sm', style: {padding: '6px 0', borderBottom: '1px solid var(--border)'}},
              h('strong', {}, event.at + ' · '), event.detail))
          : h('p', {class: 'muted'}, 'Chưa có sự kiện từ solver.'));
        clear(monitorHistory, (state.monitor_events || []).length
          ? state.monitor_events.slice().reverse().map(line =>
            h('div', {class: 'fs-sm', style: {padding: '6px 0', borderBottom: '1px solid var(--border)'}}, line))
          : h('p', {class: 'muted'}, state.monitor_error || 'Chưa có sự kiện giám sát.'));
      } catch (error) {
        if (active) clear(status, notice('danger', 'Không đọc được trạng thái', friendlyError(error)));
      } finally {refreshing = false;}
    }
    refresh();
    d.interval(refresh, 5000);
    d.add(() => { active = false; });
    return {unmount: () => d.dispose()};
  },
};
