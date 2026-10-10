import {h, clear} from '../core/dom.js';
import {request, friendlyError} from '../core/api.js';
import {bytes} from '../core/format.js';
import {btn, card, notice, select, errorBox} from '../ui/kit.js';
import {confirmDialog, toast} from '../ui/overlays.js';

const names = {'core': 'Ứng dụng Kid TE', 'gpu-cuda12': 'Tăng tốc chép lời NVIDIA'};
const phases = {check: 'Đang kiểm tra bản phát hành…', install: 'Đang tải và kiểm tra gói…', rollback: 'Đang chuẩn bị quay lại bản trước…', cancel: 'Đang hủy cập nhật chờ…'};

export async function updates(ctx, host, owner) {
  let disposed = false;
  let polling = false;
  let channel = null;
  owner.add(() => { disposed = true; });

  const act = async (action, body = {}) => {
    try {
      await request(`/api/updates/${action}`, {method: 'POST', body});
      await render();
    } catch (error) { if (!disposed) toast(friendlyError(error), 'danger'); }
  };
  async function render() {
    if (polling || disposed) return;
    polling = true;
    try {
      const state = await request('/api/updates');
      if (disposed) return;
      if (channel === null) channel = state.channel || 'stable';
      const selectChannel = select([['stable', 'Ổn định'], ['beta', 'Thử nghiệm']], channel);
      selectChannel.disabled = Boolean(state.busy);
      selectChannel.addEventListener('change', () => { channel = selectChannel.value; render(); });
      const rows = (state.channel === channel ? state.available || [] : []).map(pkg => card(names[pkg.id] || pkg.id,
        h('div', {class: 'col gap-3'},
          h('p', {}, `Phiên bản ${pkg.version} · tải ${bytes(pkg.download_bytes)} · đã cài ${state.installed?.[pkg.id] || 'chưa có gói riêng'}`),
          btn(pkg.id === 'core' ? 'Chuẩn bị cập nhật' : 'Cài / cập nhật tăng tốc NVIDIA', {
            kind: 'primary', disabled: state.busy || state.restart_required,
            onClick: async () => {
              try {
                const selection = {components: {[pkg.id]: pkg.version}};
                const plan = await request('/api/updates/plan', {method: 'POST', body: selection});
                if (disposed) return;
                const items = plan.packages.map(p => `${names[p.id] || p.id} ${p.version}: ${bytes(p.download_bytes)}`).join('; ');
                const approved = await confirmDialog({title: `Tải ${names[pkg.id] || pkg.id}?`,
                  body: `Tổng tải ${bytes(plan.download_bytes)}. ${items}. Sau khi kiểm tra, hãy đóng Kid TE và mở lại bằng biểu tượng Kid TE để áp dụng.`,
                  confirmText: 'Tải và chuẩn bị'});
                if (approved && !disposed) await act('install', selection);
              } catch (error) { if (!disposed) toast(friendlyError(error), 'danger'); }
            },
          }))));
      const progress = state.progress;
      clear(host, h('div', {class: 'col gap-4'},
        card('Cập nhật ứng dụng và tính năng', h('div', {class: 'col gap-3'},
          h('p', {}, 'Bản phát hành từ kidlost1412/KidTE. Chọn tính năng cần dùng; NVIDIA chỉ cần khi muốn tăng tốc chép lời.'),
          h('div', {class: 'row gap-3'}, selectChannel,
            btn('Kiểm tra cập nhật', {icon: 'refresh', disabled: !state.enabled || !state.trusted || state.busy,
              onClick: () => act('check', {channel})})),
          !state.enabled && notice('info', 'Chưa bật cập nhật ở bản mã nguồn', 'Dùng bản cài đã đóng gói để cập nhật.'),
          state.busy && notice('info', phases[state.operation] || 'Đang xử lý…',
            progress ? `${names[progress.component] || progress.component}: ${bytes(progress.received)} / ${bytes(progress.total)}` : ''),
          state.error && notice('danger', 'Chưa hoàn tất cập nhật', friendlyError({code: state.error})),
          state.restart_required && notice('info', 'Gói đã sẵn sàng cho lần mở tiếp theo',
            'Kết thúc phiên LIVE, đóng Kid TE rồi mở lại bằng biểu tượng Kid TE. Dữ liệu và model hiện có được giữ nguyên.'),
          h('div', {class: 'row gap-3'},
            btn('Hủy cập nhật chờ', {disabled: state.busy || !state.restart_required, onClick: () => act('cancel')}),
            btn('Quay lại bản trước', {disabled: state.busy, onClick: async () => {
              const approved = await confirmDialog({title: 'Quay lại bộ chương trình trước?',
                body: 'Thao tác được chuẩn bị cho lần mở tiếp theo. Hãy kết thúc phiên LIVE rồi đóng và mở lại Kid TE.', confirmText: 'Chuẩn bị quay lại'});
              if (approved && !disposed) await act('rollback');
            }})))),
        ...rows));
    } catch (error) {
      if (!disposed) clear(host, errorBox(friendlyError(error), render));
    } finally { polling = false; }
  }
  await render();
  owner.interval(render, 1500);
}
