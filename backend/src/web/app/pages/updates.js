import {h, clear} from '../core/dom.js';
import {request, friendlyError} from '../core/api.js';
import {bytes} from '../core/format.js';
import {btn, card, notice, select} from '../ui/kit.js';
import {confirmDialog, toast} from '../ui/overlays.js';
import {headerLines, updateComponents, progressView, OPTIONAL_IDS, LIVE_RESTART_TEXT} from '../core/update_notice.js';

const names = {'core': 'Ứng dụng Kid TE', 'app': 'Ứng dụng Kid TE', 'runtime': 'Thư viện nền', 'extensions': 'Tiện ích Chrome',
  'gpu-cuda12': 'Tăng tốc chép lời NVIDIA',
  'media-tools': 'Công cụ media (video)', 'captcha-solver': 'Giải CAPTCHA và thông báo',
  'video-sync': 'Đồng bộ video LIVE', 'video-analysis-cpu': 'Bộ chép lời trên máy'};
const phases = {check: 'Đang kiểm tra bản mới…', install: 'Đang tải và kiểm tra bản cập nhật…', rollback: 'Đang chuẩn bị quay lại bản trước…', cancel: 'Đang hủy cập nhật chờ…', remove: 'Đang chuẩn bị gỡ tính năng…'};

function progressBar(view) {
  return h('div', {class: 'col gap-2'},
    h('div', {role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-valuenow': String(view.percent),
      style: {height: '10px', borderRadius: '6px', background: 'rgba(128,128,128,.25)', overflow: 'hidden'}},
      h('div', {style: {height: '100%', width: `${view.percent}%`, background: 'var(--accent, #2563eb)', transition: 'width .3s'}})),
    h('div', {class: 'row gap-3 fs-sm'}, h('strong', {}, view.text),
      h('span', {class: 'muted'}, `Đang tải: ${names[view.component] || view.component} (${view.index}/${view.count})`)));
}

export async function updates(ctx, host, owner) {
  let disposed = false;
  let polling = false;
  let channel = null;
  let autoChecked = false;
  let restarting = false;
  const opened = {};
  owner.add(() => { disposed = true; });

  // Mục thu gọn giữ trạng thái mở qua các lần vẽ lại (trang tự làm mới mỗi 1,5 giây).
  const det = (key, title, ...children) => {
    const node = h('details', {}, h('summary', {}, title), ...children);
    node.open = Boolean(opened[key]);
    node.addEventListener('toggle', () => { opened[key] = node.open; });
    return node;
  };
  const act = async (action, body = {}) => {
    try {
      await request(`/api/updates/${action}`, {method: 'POST', body});
      await render();
    } catch (error) { if (!disposed) toast(friendlyError(error), 'danger'); }
  };
  const install = components => act('install', {components});
  const restart = async () => {
    try {
      try { await request('/api/updates/restart', {method: 'POST', body: {}}); }
      catch (error) {
        if ((error && (error.code || error.message)) !== 'live_confirm_required') throw error;
        const ok = await confirmDialog({title: 'Khởi động lại khi đang LIVE?', body: LIVE_RESTART_TEXT, confirmText: 'Khởi động lại ngay'});
        if (!ok) return;
        await request('/api/updates/restart', {method: 'POST', body: {confirm_live: true}});
      }
      restarting = true;
      await render();
    } catch (error) { if (!disposed) toast(friendlyError(error), 'danger'); }
  };

  const optionalRow = (state, pkg) => {
    const installed = state.installed?.[pkg.id];
    return h('div', {class: 'row gap-3', style: {justifyContent: 'space-between', alignItems: 'center'}},
      h('div', {}, h('strong', {}, names[pkg.id] || pkg.id),
        h('div', {class: 'muted fs-sm'}, installed ? `Đã cài ${installed}${pkg.newer ? ` · có bản ${pkg.version}` : ''}` : `Chưa cài · tải ${bytes(pkg.download_bytes)}`)),
      h('div', {class: 'row gap-2'},
        !installed && btn('Cài', {kind: 'primary', size: 'sm', disabled: state.busy || state.restart_required, onClick: async () => {
          try {
            const plan = await request('/api/updates/plan', {method: 'POST', body: {components: {[pkg.id]: pkg.version}}});
            if (disposed) return;
            const approved = await confirmDialog({title: `Cài ${names[pkg.id] || pkg.id}?`,
              body: `Tổng tải ${bytes(plan.download_bytes)}. Tải xong, bấm "Khởi động lại ngay" để dùng tính năng.`, confirmText: 'Tải và cài'});
            if (approved && !disposed) await install({[pkg.id]: pkg.version});
          } catch (error) { if (!disposed) toast(friendlyError(error), 'danger'); }
        }}),
        installed && btn('Gỡ', {size: 'sm', disabled: state.busy || state.restart_required, onClick: async () => {
          const approved = await confirmDialog({title: `Gỡ ${names[pkg.id] || pkg.id}?`,
            body: 'Tính năng sẽ tắt sau khi khởi động lại Kid TE. Dữ liệu được giữ.', confirmText: 'Gỡ tính năng'});
          if (approved && !disposed) await act('remove', {component: pkg.id});
        }})));
  };

  async function render() {
    if (polling || disposed) return;
    polling = true;
    try {
      const state = await request('/api/updates');
      if (disposed) return;
      if (channel === null) channel = state.channel || 'beta';
      // Tự kiểm tra một lần khi mở trang để người dùng không phải bấm "Kiểm tra".
      if (!autoChecked && state.enabled && state.trusted && !state.busy && !state.restart_required && !state.last_checked_at) {
        autoChecked = true;
        request('/api/updates/check', {method: 'POST', body: {channel}}).catch(() => {});
      }
      const head = headerLines(state);
      const view = progressView(state);
      const comps = updateComponents(state);
      const hasUpdate = state.update_available && Object.keys(comps).length > 0;
      const selectChannel = select([['beta', 'Thử nghiệm'], ['stable', 'Ổn định']], channel);
      selectChannel.disabled = Boolean(state.busy);
      selectChannel.addEventListener('change', () => { channel = selectChannel.value; act('check', {channel}); });
      const rows = state.channel === channel ? state.available || [] : [];
      const optional = rows.filter(p => OPTIONAL_IDS.includes(p.id));
      const display = state.installed_display || {};

      const main = [h('div', {class: 'col gap-1'}, h('strong', {style: {fontSize: '1.15em'}}, head.using),
        head.newest && h('div', {}, head.newest),
        head.upToDate && h('div', {class: 'ok'}, head.upToDateText),
        !state.enabled && h('div', {class: 'muted'}, 'Bản mã nguồn không tự cập nhật; hãy dùng bản cài đã đóng gói.'),
        state.enabled && !head.newest && !state.busy && !state.error && h('div', {class: 'muted'}, 'Đang kiểm tra bản mới nhất trên GitHub…'))];

      if (state.error) {
        main.push(notice('danger', 'Chưa cập nhật được', friendlyError({code: state.error}),
          btn('Thử lại', {kind: 'primary', size: 'sm', disabled: state.busy, onClick: () => act('check', {channel})})));
      }
      if (state.busy && state.operation === 'install' && view) main.push(notice('info', phases.install, ''), progressBar(view));
      else if (state.busy) main.push(notice('info', phases[state.operation] || 'Đang xử lý…', ''));
      if (restarting) {
        main.push(notice('info', 'Đang khởi động lại…', 'Kid TE sẽ tự mở lại sau khoảng 20–30 giây. Có thể đóng trang này.'));
      } else if (state.restart_required && !state.busy) {
        main.push(notice('ok', `Đã tải xong bản ${state.latest?.version || ''}`.trim(),
          'Khởi động lại để áp dụng. Dữ liệu và model hiện có được giữ nguyên.',
          btn('Khởi động lại ngay', {kind: 'primary', onClick: restart})));
      } else if (hasUpdate && !state.busy) {
        main.push(h('div', {class: 'row gap-3', style: {alignItems: 'center'}},
          btn('Cập nhật', {kind: 'primary', icon: 'refresh', disabled: !state.enabled, onClick: () => install(comps)}),
          h('span', {class: 'muted'}, `Sẽ tải khoảng ${bytes(state.update_total_bytes || 0)}, tự kiểm tra, rồi hỏi bạn khởi động lại.`)));
      }

      const optionalSection = det('optional', 'Tính năng tuỳ chọn', h('div', {class: 'col gap-3', style: {paddingTop: '10px'}},
        h('p', {class: 'muted fs-sm'}, 'Chỉ cài khi cần (ví dụ NVIDIA để tăng tốc chép lời). Tính năng đã cài tự được cập nhật cùng nút "Cập nhật".'),
        optional.length ? optional.map(p => optionalRow(state, p)) : h('p', {class: 'muted'}, 'Bấm "Kiểm tra lại" trong mục Nâng cao để xem danh sách.')));
      const techSection = det('tech', 'Chi tiết kỹ thuật', h('div', {class: 'col gap-2', style: {paddingTop: '10px'}},
        h('p', {class: 'muted fs-sm'}, `Kênh: ${state.channel}. Kiểm tra lúc: ${state.last_checked_at || 'chưa'}.`),
        ...rows.filter(p => !OPTIONAL_IDS.includes(p.id)).map(p => h('div', {class: 'fs-sm'},
          `${p.id}: đang cài ${display[p.id] || '— (chưa có gói riêng)'} → bản trong danh mục ${p.version}${p.newer ? ' (mới)' : ''} · ${bytes(p.download_bytes)}`)),
        state.pending && Object.keys(state.pending).length
          ? h('div', {class: 'fs-sm'}, `Đang chờ áp dụng: ${Object.entries(state.pending).map(([k, v]) => `${k} ${v}`).join(', ')}`) : null));
      const advanced = det('advanced', 'Nâng cao', h('div', {class: 'col gap-3', style: {paddingTop: '10px'}},
        h('div', {class: 'row gap-3'}, selectChannel,
          btn('Kiểm tra lại', {icon: 'refresh', disabled: !state.enabled || !state.trusted || state.busy, onClick: () => act('check', {channel})})),
        h('div', {class: 'row gap-3'},
          btn('Hủy cập nhật chờ', {disabled: state.busy || !state.restart_required, onClick: () => act('cancel')}),
          btn('Quay lại bản trước', {disabled: state.busy, onClick: async () => {
            const approved = await confirmDialog({title: 'Quay lại bộ chương trình trước?',
              body: 'Thao tác được áp dụng ở lần mở tiếp theo. Hãy đóng và mở lại Kid TE.', confirmText: 'Chuẩn bị quay lại'});
            if (approved && !disposed) await act('rollback');
          }}))));

      clear(host, h('div', {class: 'col gap-4'},
        card('Cập nhật Kid TE', h('div', {class: 'col gap-3'}, ...main.filter(Boolean))),
        card(null, h('div', {class: 'col gap-3'}, optionalSection, techSection, advanced))));
    } catch (error) {
      if (!disposed && !restarting) clear(host, notice('danger', 'Chưa tải được trang cập nhật', friendlyError(error), btn('Thử lại', {size: 'sm', onClick: render})));
    } finally { polling = false; }
  }
  await render();
  owner.interval(render, 1500);
}
