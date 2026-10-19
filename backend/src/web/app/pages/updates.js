import {h, clear} from '../core/dom.js';
import {request, friendlyError} from '../core/api.js';
import {bytes} from '../core/format.js';
import {btn, chip, notice, progress, select} from '../ui/kit.js';
import {icon} from '../core/icons.js';
import {confirmDialog, toast} from '../ui/overlays.js';
import {headerLines, updateComponents, allUpdateComponents, progressView, formatCheckedAt, OPTIONAL_INFO, OPTIONAL_IDS, LIVE_RESTART_TEXT, launcherNotice} from '../core/update_notice.js';

const names = {'core': 'Ứng dụng Kid TE', 'app': 'Ứng dụng Kid TE', 'runtime': 'Thư viện nền', 'extensions': 'Tiện ích Chrome',
  'gpu-cuda12': 'Tăng tốc chép lời NVIDIA',
  'media-tools': 'Công cụ media (video)', 'captcha-solver': 'Giải CAPTCHA và thông báo',
  'video-sync': 'Đồng bộ video LIVE', 'video-analysis-cpu': 'Bộ chép lời trên máy'};
const phases = {check: 'Đang kiểm tra bản mới…', install: 'Đang tải và kiểm tra bản cập nhật…', rollback: 'Đang chuẩn bị quay lại bản trước…', cancel: 'Đang hủy cập nhật chờ…', remove: 'Đang chuẩn bị gỡ tính năng…'};

function progressBar(view) {
  return h('div', {class: 'upd-progress'},
    progress(view.percent),
    h('div', {class: 'row gap-3 fs-sm'}, h('strong', {}, view.text),
      h('span', {class: 'muted'}, `Đang tải: ${names[view.component] || view.component} (${view.index}/${view.count})`)));
}

export async function updates(ctx, host, owner) {
  let disposed = false;
  let polling = false;
  let channel = null;
  let autoChecked = false;
  let restarting = false;
  let planning = false;
  const opened = {};
  owner.add(() => { disposed = true; });

  // Mục thu gọn giữ trạng thái mở qua các lần vẽ lại (trang tự làm mới mỗi 1,5 giây).
  const det = (key, title, {sub, iconName, count} = {}, ...children) => {
    const node = h('details', {class: 'upd-sec'},
      h('summary', {}, iconName ? icon(iconName) : null,
        h('span', {class: 'upd-sec-title'}, title, sub ? h('small', {}, sub) : null),
        count ? chip(String(count), '', {size: 'sm'}) : null,
        h('span', {class: 'upd-chev'}, icon('chevronDown'))),
      h('div', {class: 'upd-sec-body'}, ...children));
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
  const installAll = async state => {
    if (planning) return;
    const components = allUpdateComponents(state);
    if (!Object.keys(components).length) return;
    planning = true;
    try {
      await render();
      const plan = await request('/api/updates/plan', {method: 'POST', body: {components}});
      if (disposed) return;
      const list = plan.packages.map(p => names[p.id] || p.id).join(', ');
      const approved = await confirmDialog({title: 'Cập nhật toàn bộ?',
        text: `Gồm: ${list}. Tổng tải ${bytes(plan.download_bytes)}. Tải và kiểm tra tất cả trong một lượt, sau đó chỉ khởi động lại một lần.`,
        confirm: 'Tải và cài toàn bộ'});
      if (approved && !disposed) await install(components);
    } catch (error) { if (!disposed) toast(friendlyError(error), 'danger'); }
    finally { planning = false; if (!disposed) await render(); }
  };
  const restart = async () => {
    try {
      try { await request('/api/updates/restart', {method: 'POST', body: {}}); }
      catch (error) {
        if ((error && (error.code || error.message)) !== 'live_confirm_required') throw error;
        const ok = await confirmDialog({title: 'Khởi động lại khi đang LIVE?', text: LIVE_RESTART_TEXT, confirm: 'Khởi động lại ngay'});
        if (!ok) return;
        await request('/api/updates/restart', {method: 'POST', body: {confirm_live: true}});
      }
      restarting = true;
      await render();
    } catch (error) { if (!disposed) toast(friendlyError(error), 'danger'); }
  };

  const optionalRow = (state, pkg) => {
    const installed = state.installed?.[pkg.id];
    const info = OPTIONAL_INFO[pkg.id] || {icon: 'box', text: ''};
    const status = installed ? `Đã cài ${installed}${pkg.newer ? ` · có bản ${pkg.version}` : ''}` : `Chưa cài · tải ${bytes(pkg.download_bytes)}`;
    return h('div', {class: 'upd-row'},
      h('span', {class: 'upd-row-icon'}, icon(info.icon)),
      h('div', {class: 'upd-row-main'}, h('strong', {}, names[pkg.id] || pkg.id),
        h('div', {class: 'muted fs-sm'}, info.text ? `${info.text}. ${status}` : status)),
      chip(installed ? `v${installed}` : `v${pkg.version}`, installed ? 'ok' : '', {size: 'sm'}),
      !installed && btn('Cài', {kind: 'primary', size: 'sm', disabled: planning || state.busy || state.restart_required, onClick: async () => {
        try {
          const plan = await request('/api/updates/plan', {method: 'POST', body: {components: {[pkg.id]: pkg.version}}});
          if (disposed) return;
          const approved = await confirmDialog({title: `Cài ${names[pkg.id] || pkg.id}?`,
            text: `Tổng tải ${bytes(plan.download_bytes)}. Tải xong, bấm "Khởi động lại ngay" để dùng tính năng.`, confirm: 'Tải và cài'});
          if (approved && !disposed) await install({[pkg.id]: pkg.version});
        } catch (error) { if (!disposed) toast(friendlyError(error), 'danger'); }
      }}),
      installed && btn('Gỡ', {kind: 'ghost', size: 'sm', disabled: planning || state.busy || state.restart_required, onClick: async () => {
        const approved = await confirmDialog({title: `Gỡ ${names[pkg.id] || pkg.id}?`,
          text: 'Tính năng sẽ tắt sau khi khởi động lại Kid TE. Dữ liệu được giữ.', confirm: 'Gỡ tính năng'});
        if (approved && !disposed) await act('remove', {component: pkg.id});
      }}));
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
      selectChannel.disabled = Boolean(planning || state.busy);
      selectChannel.addEventListener('change', () => { channel = selectChannel.value; act('check', {channel}); });
      const rows = state.channel === channel ? state.available || [] : [];
      const optional = rows.filter(p => OPTIONAL_IDS.includes(p.id));
      const allComps = state.channel === channel ? allUpdateComponents(state) : {};
      const showAll = !restarting && !state.restart_required && Object.keys(allComps).length > 0;
      const display = state.installed_display || {};

      const current = state.current_version || '';
      const checkedAt = formatCheckedAt(state.last_checked_at);
      const checking = state.enabled && !head.newest && !state.busy && !state.error;
      const recheck = btn('Kiểm tra lại', {kind: 'ghost', size: 'sm', icon: 'refresh', disabled: planning || !state.enabled || !state.trusted || state.busy, onClick: () => act('check', {channel})});
      const mb = Math.max(1, Math.round((state.update_total_bytes || 0) / 1e6));
      const showUpdate = !restarting && !state.restart_required && hasUpdate && !state.busy;
      const hero = [h('div', {class: 'upd-hero-top'},
        h('div', {class: 'upd-hero-ver'},
          h('small', {}, 'Đang dùng'),
          h('strong', {}, current ? `Kid TE ${current}` : 'Kid TE'),
          head.newest && h('span', {class: 'muted fs-sm'}, head.newest)),
        h('div', {class: 'upd-hero-act'},
          head.upToDate && chip('Đã là bản mới nhất', 'ok', {iconName: 'check'}),
          showAll && btn(planning ? 'Đang tính tổng tải…' : 'Cập nhật toàn bộ', {kind: 'primary', icon: 'download', disabled: planning || state.hardware_probing || state.busy || !state.enabled || !state.trusted, onClick: () => installAll(state)}),
          showUpdate && btn(`Chỉ cập nhật gói đã cài (~${mb} MB)`, {kind: 'ghost', disabled: planning || !state.enabled, onClick: () => install(comps)}))),
        h('div', {class: 'upd-hero-meta muted fs-sm'},
          checking ? 'Đang kiểm tra bản mới nhất trên GitHub…' : (checkedAt ? `Kiểm tra lần cuối: ${checkedAt}` : 'Chưa kiểm tra'), recheck),
        !state.enabled && h('div', {class: 'muted fs-sm'}, 'Bản mã nguồn không tự cập nhật; hãy dùng bản cài đã đóng gói.'),
        showAll && h('div', {class: 'muted fs-sm'}, 'Cài các tính năng còn thiếu và cập nhật gói đã cài trong một lượt. Chỉ khởi động lại một lần sau khi tải xong.'),
        h('div', {class: 'muted fs-sm'}, state.hardware_probing ? 'Đang nhận diện card đồ họa…' : state.setup_hardware?.eligible ? `Đã nhận diện ${state.setup_hardware.name || 'card NVIDIA'} — tự chọn gói tăng tốc NVIDIA.` : 'Dùng CPU; gói tăng tốc NVIDIA không được tự tải.')];
      const main = [];
      const launcherOld = launcherNotice(state);
      if (launcherOld) {
        main.push(notice('warn', 'Trình khởi động đã cũ', launcherOld.text,
          launcherOld.url && btn('Mở trang tải bộ cài', {kind: 'ghost', size: 'sm', onClick: () => window.open(launcherOld.url, '_blank', 'noopener')})));
      }
      const failure = state.last_update_failure;
      if (failure && failure.message) {
        // Bản cập nhật trước bị launcher tự lùi: báo rõ, bấm "Đã hiểu" để ẩn.
        main.push(notice('warn', 'Lần cập nhật trước chưa thành công', failure.message,
          btn('Đã hiểu', {kind: 'ghost', size: 'sm', onClick: async () => {
            try { await request('/api/updates/ack_update_failure', {method: 'POST', body: {}}); }
            catch (error) { toast(friendlyError(error), 'danger'); return; }
            if (!disposed) render();
          }})));
      }
      if (state.error) {
        main.push(notice('danger', 'Chưa cập nhật được', friendlyError({code: state.error}),
          btn('Thử lại', {kind: 'primary', size: 'sm', disabled: state.busy, onClick: () => act('check', {channel})})));
      }
      if (state.catalog_expiring_soon) {
        const left = Math.max(0, state.catalog_days_left ?? 0);
        main.push(h('div', {class: 'muted fs-sm'}, `Danh mục cập nhật còn ${left} ngày là hết hạn. Hãy báo người quản trị gia hạn để máy vẫn nhận được bản mới.`));
      }
      if (state.busy && state.operation === 'install' && view) main.push(progressBar(view));
      else if (state.busy) main.push(notice('info', state.operation === 'initial_setup' ? 'Đang tự thiết lập máy mới…' : phases[state.operation] || 'Đang xử lý…', state.initial_setup ? 'Tự nhận diện phần cứng và tải các tính năng cần thiết. Chỉ khởi động lại một lần khi hoàn tất.' : ''));
      if (restarting) {
        main.push(notice('info', 'Đang khởi động lại…', 'Kid TE sẽ tự mở lại sau khoảng 20–30 giây. Có thể đóng trang này.'));
      } else if (state.restart_required && !state.busy) {
        main.push(h('div', {class: 'upd-done'},
          h('span', {class: 'upd-done-icon'}, icon('check')),
          h('div', {class: 'grow'}, h('strong', {}, `Đã tải xong bản ${state.latest?.version || ''}`.trim()),
            h('div', {class: 'muted fs-sm'}, 'Khởi động lại để áp dụng. Dữ liệu và model hiện có được giữ nguyên.')),
          btn('Khởi động lại ngay', {kind: 'primary', onClick: restart})));
      }

      const optionalSection = det('optional', 'Tính năng tuỳ chọn', {iconName: 'layers', sub: 'Chỉ cài khi cần', count: optional.length}, h('div', {class: 'col gap-2'},
        h('p', {class: 'muted fs-sm'}, 'Dùng "Cập nhật toàn bộ" ở trên để cài các tính năng cùng lúc, hoặc cài riêng từng gói bên dưới. Gói NVIDIA được tự chọn theo card và driver trên máy.'),
        optional.length ? optional.map(p => optionalRow(state, p)) : h('p', {class: 'muted'}, 'Bấm "Kiểm tra lại" ở phần trên để xem danh sách.')));
      const techSection = det('tech', 'Chi tiết kỹ thuật', {iconName: 'terminal'}, h('div', {class: 'col gap-2'},
        h('p', {class: 'muted fs-sm'}, `Kênh: ${state.channel}. Kiểm tra lúc: ${checkedAt || 'chưa'}.`),
        ...rows.filter(p => !OPTIONAL_IDS.includes(p.id)).map(p => h('div', {class: 'fs-sm'},
          `${p.id}: đang cài ${display[p.id] || '— (chưa có gói riêng)'} → bản trong danh mục ${p.version}${p.newer ? ' (mới)' : ''} · ${bytes(p.download_bytes)}`)),
        state.pending && Object.keys(state.pending).length
          ? h('div', {class: 'fs-sm'}, `Đang chờ áp dụng: ${Object.entries(state.pending).map(([k, v]) => `${k} ${v}`).join(', ')}`) : null));
      const advanced = det('advanced', 'Nâng cao', {iconName: 'settings'}, h('div', {class: 'col gap-3'},
        h('div', {class: 'upd-field'}, h('span', {class: 'muted fs-sm'}, 'Kênh cập nhật'), selectChannel),
        h('div', {class: 'row gap-3'},
          btn('Hủy cập nhật chờ', {disabled: state.busy || !state.restart_required, onClick: () => act('cancel')}),
          btn('Quay lại bản trước', {disabled: state.busy, onClick: async () => {
            const approved = await confirmDialog({title: 'Quay lại bộ chương trình trước?',
              text: 'Thao tác được áp dụng ở lần mở tiếp theo. Hãy đóng và mở lại Kid TE.', confirm: 'Chuẩn bị quay lại'});
            if (approved && !disposed) await act('rollback');
          }}))));

      clear(host, h('div', {class: 'col gap-4 upd'},
        h('section', {class: 'card upd-hero'}, ...hero.filter(Boolean), ...main.filter(Boolean)),
        h('section', {class: 'card upd-list'}, optionalSection, techSection, advanced)));
    } catch (error) {
      if (!disposed && !restarting) clear(host, notice('danger', 'Chưa tải được trang cập nhật', friendlyError(error), btn('Thử lại', {size: 'sm', onClick: render})));
    } finally { polling = false; }
  }
  await render();
  owner.interval(render, 1500);
}
