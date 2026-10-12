import {h, clear, disposer} from '../core/dom.js';
import {request, friendlyError} from '../core/api.js';
import {btn, errorBox, skeletonRows, pageHead, field, input, textarea, card} from '../ui/kit.js';
import {toast} from '../ui/overlays.js';

export default {
  mount(ctx) {
    const d = disposer();
    const host = h('div', {style: {marginTop: '16px'}}, shopInfoCard());
    clear(ctx.shell.main, h('div', {class: 'page'},
      pageHead('Thông tin shop', 'Thông tin công khai AI dùng khi khách hỏi địa chỉ, hotline và giao hàng.'), host));
    return {unmount: () => d.dispose()};
  },
};

// Thông tin CÔNG KHAI của shop — AI dùng khi khách hỏi ở đâu, số điện thoại, giao hàng.
function shopInfoCard() {
  const body = h('div', {class: 'card-body form-grid'}, skeletonRows(3, 40));
  const inputs = {};
  const save = btn('Lưu thông tin shop', {kind: 'primary', icon: 'save', onClick: async () => {
    const values = Object.fromEntries(Object.entries(inputs).map(([k, el]) => [k, el.value.trim()]));
    try { await request('/api/system/shop-info', {method: 'POST', body: {values}}); toast('Đã lưu — AI dùng ngay cho câu tiếp theo.', 'ok'); }
    catch (e) { toast(friendlyError(e), 'danger'); }
  }});
  request('/api/system/shop-info').then(out => {
    clear(body, h('p', {class: 'fs-sm soft'}, 'Chỉ điền thông tin công khai. Khách hỏi “shop ở đâu”, “số điện thoại”, “ship mấy ngày” thì AI trả lời theo đây; để trống thì AI không trả lời những câu đó.'),
      out.fields.map(([key, label, hint]) => {
        const long = key === 'cach_noi' || key === 'giao_hang' || key === 'dia_chi_mua_hang';
        inputs[key] = long ? textarea({rows: 2, value: out.values[key] || '', placeholder: hint})
          : input({value: out.values[key] || '', placeholder: hint});
        return field(label, inputs[key]);
      }));
  }).catch(e => clear(body, errorBox(friendlyError(e))));
  return card('Thông tin shop', body, {sub: 'Địa chỉ, hotline, giao hàng', footer: save});
}

