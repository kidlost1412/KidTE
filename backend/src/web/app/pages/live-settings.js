import {h, clear} from '../core/dom.js';
import {request, friendlyError} from '../core/api.js';
import {btn, card, field, input, select, toggle, notice, errorBox} from '../ui/kit.js';
import {toast} from '../ui/overlays.js';
import {liveChannelCards} from '../ui/live-status.js';
import {audioProblems} from './audio-report-model.js';

export async function liveSettings(ctx, host, d) {
  let data, schedules, devices;
  const statusSlot = h('div');
  let channelGrid;
  const sourceRefreshers = [];
  async function refresh() {
    const fresh = await request('/api/live/sources');
    const names = Object.fromEntries(Object.entries(schedules?.timelines || {}).map(([id, item]) => [id, item.title]));
    if (!channelGrid) { channelGrid = liveChannelCards(ctx,fresh.channels,names); clear(statusSlot,channelGrid); }
    else channelGrid.update(fresh.channels,names);
    sourceRefreshers.forEach(fn => fn(fresh.channels.shopee));
    return fresh;
  }
  try {
    [data, schedules, devices] = await Promise.all([request('/api/live/sources'), request('/api/video/timelines'), request('/api/settings')]);
  } catch (error) { clear(host, errorBox(friendlyError(error), () => liveSettings(ctx, host, d))); return; }
  const panels = ['shopee', 'tiktok'].map(platform => {
    const channel = data.channels[platform], settings = channel.settings;
    let enabled = settings.enabled;
    const offset = input({type: 'number', value: settings.offset_ms, min: -60000, max: 60000, step: 100});
    const controls = [toggle('Bật nhận diện cho sàn này', enabled, value => {enabled = value;})];
    let mode, device, processName, port, password, obsInput, selectedVideo, selectedMedia;
    if (platform === 'shopee') {
      const sources = channel.health.inputs || [];
      port = input({type: 'number', value: settings.port, min: 1024, max: 65535});
      password = input({type: 'password', autocomplete: 'off', placeholder: settings.password_configured ? 'Đã lưu — để trống nếu không đổi' : 'Mật khẩu OBS WebSocket'});
      obsInput = select([['', 'Tự chọn khi chỉ có một Media Source đang phát'], ...sources.map(item => [item.input_name, item.input_name])], settings.input_name);
      selectedMedia = select([['', 'Chọn file Media Source để liên kết'], ...sources.map(item => [item.media_path, item.input_name + ' · ' + item.media_path])], '');
      selectedVideo = select([['', 'Video tương ứng trong thư viện'], ...Object.entries(schedules.timelines || {}).map(([id, item]) => [id, item.title || id])], '');
      let sourcesKey = JSON.stringify(sources);
      sourceRefreshers.push(fresh => {
        Object.assign(settings.bindings, fresh.settings.bindings);
        const items = fresh.health.inputs || [];
        const key = JSON.stringify(items.map(item => [item.input_name, item.media_path]));
        if (key === sourcesKey) return;
        sourcesKey = key;
        const inputValue = obsInput.value, mediaValue = selectedMedia.value;
        clear(obsInput, h('option', {value: ''}, 'Tự chọn khi chỉ có một Media Source đang phát'),
          items.map(item => h('option', {value: item.input_name}, item.input_name)));
        clear(selectedMedia, h('option', {value: ''}, 'Chọn file Media Source để liên kết'),
          items.map(item => h('option', {value: item.media_path}, item.input_name + ' · ' + item.media_path)));
        if (inputValue && !items.some(item => item.input_name === inputValue)) obsInput.append(h('option', {value: inputValue}, inputValue + ' · chưa thấy nguồn'));
        if (mediaValue && !items.some(item => item.media_path === mediaValue)) selectedMedia.append(h('option', {value: mediaValue}, mediaValue));
        obsInput.value = inputValue; selectedMedia.value = mediaValue;
      });
      controls.push(notice('info', 'OBS → Shopee', 'Bật Máy chủ WebSocket trong OBS → Công cụ. Kid TE đối chiếu vân tay âm thanh của file với thư viện và đọc thời điểm phát từ nguồn phương tiện ở cảnh đang lên sóng. Nếu không nhận được, liên kết file với video một lần.'),
        field('Cổng OBS WebSocket', port), field('Mật khẩu kết nối', password), field('Nguồn phương tiện Shopee', obsInput),
        field('Liên kết file OBS', selectedMedia), field('Với video Kid TE', selectedVideo));
    } else {
      mode = select([['process', 'Nghe riêng TikTok Studio (khuyên dùng)'], ['device', 'Thiết bị âm thanh riêng đã tách cho TikTok']], settings.mode);
      processName = input({value: settings.process_name});
      device = select([['', 'Chọn nguồn âm riêng'], ...(devices.audio_devices || []).filter(item => item.loopback && !item.disabled).map(item => [item.id, item.name])], settings.device_id);
      controls.push(notice('info', 'TikTok Studio → TikTok', 'Kid TE thu riêng âm thanh của TikTok Studio và các tiến trình con. Nếu Studio không xuất âm thanh thu được, chọn một thiết bị âm thanh dành riêng cho TikTok; không dùng đường loa đang trộn cả hai LIVE.'),
        ...audioProblems(devices.audio_report, settings.mode).map(p => notice(p.kind, p.title, p.text)),
        field('Cách nhận diện', mode), field('Tên ứng dụng', processName), field('Thiết bị dự phòng được chọn riêng', device));
      const suggested = devices.audio_report?.suggested_device;
      if (suggested) controls.push(btn('Dùng loa mặc định: ' + suggested.name, {onClick: () => { mode.value = 'device'; if (![...device.options].some(o => o.value === suggested.id)) device.append(h('option', {value: suggested.id}, suggested.name)); device.value = suggested.id; toast('Đã điền loa mặc định — bấm Lưu nguồn TikTok để áp dụng.', 'ok'); }}));
      controls.push(btn('Quét lại thiết bị', {onClick: async () => { try { await request('/api/audio/rescan'); liveSettings(ctx, host, d); } catch (error) { toast(friendlyError(error), 'danger'); } }}));
      controls.push(btn('Nghe lại video TikTok', {onClick: async event => {
        const button = event.currentTarget; button.disabled = true;
        try {
          await request('/api/live/reidentify', {method: 'POST', body: {platform: 'tiktok'}});
          toast('Đã yêu cầu nghe lại. Đang chờ hai đoạn âm thanh xác nhận video.', 'ok');
        } catch (error) { toast(friendlyError(error), 'danger'); }
        finally { button.disabled = false; }
      }}));
    }
    controls.push(field('Bù vị trí (mili giây)', offset), h('p', {class: 'fs-xs muted'}, 'Giá trị âm lùi ghim so với nguồn phát; giá trị dương tiến lên. Mỗi sàn giữ giá trị riêng.'));
    return card(platform === 'shopee' ? 'Nguồn Shopee' : 'Nguồn TikTok', h('div', {class: 'card-body col gap-3'}, controls),
      {footer: btn('Lưu nguồn ' + (platform === 'shopee' ? 'Shopee' : 'TikTok'), {kind: 'primary', onClick: async event => {
        const button = event.currentTarget; button.disabled = true;
        try {
          let patch = {enabled, offset_ms: Number(offset.value)};
          if (platform === 'shopee') {
            patch = {...patch, mode: 'obs', port: Number(port.value), input_name: obsInput.value};
            if (password.value) patch.password = password.value;
            if (selectedMedia.value || selectedVideo.value) {
              if (!selectedMedia.value || !selectedVideo.value) throw new Error('Chọn cả file OBS và video tương ứng.');
              patch.bindings = {...settings.bindings, [selectedMedia.value]: selectedVideo.value};
            }
          } else patch = {...patch, mode: mode.value, process_name: processName.value.trim(), device_id: device.value};
          await request('/api/live/sources', {method: 'POST', body: {platform, settings: patch}});
          toast('Đã lưu. Nguồn của sàn này sẽ tự nối lại.', 'ok');
          password && (password.value = '');
          await refresh();
        } catch (error) { toast(error.message || friendlyError(error), 'danger'); }
        finally { button.disabled = false; }
      }})});
  });
  clear(host, h('div', {class: 'col gap-4'},
    notice('info', 'Hai sàn có thể phát hai video khác nhau', 'Thư viện video, khách và kiến thức dùng chung. Nguồn phát, vị trí video, độ trễ, phiên LIVE và điều khiển ghim/AI tách theo từng sàn.'),
    statusSlot, h('div', {class: 'settings-grid'}, panels)));
  await refresh();
  d.interval(() => refresh().catch(() => {}), 3000);
}
