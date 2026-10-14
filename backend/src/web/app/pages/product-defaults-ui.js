import {h, clear} from '../core/dom.js';
import {request, friendlyError} from '../core/api.js';
import {btn, field, input, notice} from '../ui/kit.js';
import {openModal, toast} from '../ui/overlays.js';
import {invalidateCatalog} from './video/data.js';

export function defaultProfilesPanel(ctx, {onDone, initial, firstRun=false}={}) {
  const node=h('div',{class:'card card-body col gap-3'});
  const source=input({placeholder:'Để trống dùng bộ đi kèm, hoặc dán đường dẫn file bộ hồ sơ đã lưu (.json)'});
  let busy=false;
  async function choose(mode) {
    if(busy)return;
    busy=true;
    for(const b of node.querySelectorAll('button')) b.disabled=true;
    try {
      const out=await request('/api/ingest/defaults/choose',{method:'POST',body:{mode,source:source.value}});
      invalidateCatalog();toast(mode==='default'?`Đã nạp ${out.products} hồ sơ mặc định kèm ảnh. Gắn hồ sơ vào sản phẩm đang bán để sử dụng.`:'Đã chọn tự thêm hồ sơ sản phẩm.','ok');
      onDone?.();if(firstRun)ctx.navigate('catalog','profiles');else load();
    }catch(e){toast(friendlyError(e),'danger');}
    finally{busy=false;for(const b of node.querySelectorAll('button'))b.disabled=false;}
  }
  function paint(status) {
    clear(node,h('strong',{},status.needs_choice?'Chọn cách bắt đầu hồ sơ sản phẩm':'Bộ hồ sơ sản phẩm mặc định'),
      h('p',{class:'fs-sm muted'},`${status.products} hồ sơ đã chốt · ${status.images} ảnh. Bộ hồ sơ giữ riêng quy cách và thành phần combo; không mang giá, khoá API hay mã sản phẩm của shop sang máy khác.`),
      field('Bộ hồ sơ từ máy khác (không bắt buộc)',source),
      h('div',{class:'row wrap'},btn('Dùng bộ sản phẩm mặc định',{kind:'primary',disabled:!status.products,onClick:()=>choose('default')}),
        status.needs_choice||firstRun?btn('Tự thêm sản phẩm',{kind:'ghost',onClick:()=>choose('custom')}):null,
        !firstRun?btn('Lưu bộ hồ sơ đã chốt',{icon:'save',onClick:async e=>{
          const clicked=e.currentTarget; clicked.disabled=true;
          try {
            const out=await request('/api/ingest/defaults/save',{method:'POST',body:{}});
            clear(output,notice('ok',`Đã lưu ${out.products} hồ sơ và ${out.images} ảnh`,out.path),
              out.excluded?.length?notice('warn','Có combo cần xem lại, chưa đưa vào mặc định',out.excluded.map(e=>e.error).join(' · ')):null);
            load();
          }catch(error){toast(friendlyError(error),'danger');}finally{clicked.disabled=false;}
        }}):null),output);
    source.addEventListener('input',()=>{const button=[...node.querySelectorAll('button')].find(b=>b.textContent.includes('Dùng bộ'));if(button&&!busy)button.disabled=!status.products&&!source.value.trim();});
  }
  const output=h('div',{class:'col gap-3'});
  async function load(){try{const status=await request('/api/ingest/defaults-status');if(node.isConnected||initial)paint(status);}catch(e){clear(node,notice('warn','Chưa đọc được bộ mặc định',friendlyError(e)));}}
  if(initial)paint(initial);else load();
  return node;
}

export async function offerDefaultProfiles(ctx) {
  try {
    const status=await request('/api/ingest/defaults-status');
    if(!status.needs_choice)return;
    let modal;
    const body=defaultProfilesPanel(ctx,{firstRun:true,initial:status,onDone:()=>modal.close()});
    modal=openModal({title:'Hồ sơ sản phẩm cho máy mới',description:'Chọn bộ hồ sơ đã chốt hoặc tự thêm sản phẩm của anh.',body});
  }catch(_){}
}
