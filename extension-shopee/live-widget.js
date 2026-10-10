/* Shared shadow DOM monitor; never modifies the platform's LIVE controls. */
(() => {
  globalThis.KidTELiveWidget = {
    create(platform, id) {
      const host = document.createElement('div');
      host.id = id;
      const root = host.attachShadow({mode: 'open'});
      root.innerHTML = `<style>
      /* SAKURA GENERATED START */
:host {
  color-scheme: light;
  --bg: #FCF9F7;
  --bg-sunken: #F4EAEB;
  --surface: #FFFEFD;
  --surface-2: #FCF6F7;
  --surface-3: #F7ECEF;
  --surface-hover: #FBEFF0;
  --surface-active: #F8DFE5;
  --border: #E4CCD2;
  --border-strong: #CBA9B4;
  --divider: #F0E2E6;
  --text: #3C2D32;
  --text-2: #59434D;
  --text-3: #715961;
  --text-disabled: #86727A;
  --brand: #F8DFE5;
  --brand-hover: #F2D2DC;
  --brand-press: #ECC4D1;
  --brand-soft: #FBEFF0;
  --brand-soft-2: #F8DFE5;
  --brand-text: #79545E;
  --brand-on: #3C2D32;
  --brand-border: #A77B8A;
  --focus: #79545E;
  --stage-start: #F8DFE5;
  --stage-end: #FBEFF0;
  --stage-text: #3C2D32;
  --stage-muted: #715961;
  --ok: #47755F;
  --ok-soft: #EDF5F0;
  --ok-text: #386252;
  --warn: #A66B22;
  --warn-soft: #FBF1DF;
  --warn-text: #80511C;
  --danger: #BB4351;
  --danger-soft: #FCECEF;
  --danger-text: #9E3345;
  --info: #576F9B;
  --info-soft: #EDF1F8;
  --info-text: #415A85;
  --ai: #796088;
  --ai-soft: #F2EDF7;
  --ai-text: #674877;
  --live: #B23C54;
  --live-soft: #FBE8ED;
  --studio-accent: #F0B6CC;
  --danger-on: #FFFFFF;
  --live-on: #FFFFFF;
  --warn-on: #FFFFFF;
  --info-on: #FFFFFF;
  --ai-on: #FFFFFF;
  --ink: var(--text);
  --muted: var(--text-3);
  --line: var(--border);
  --soft: var(--brand-soft);
}
@media (prefers-color-scheme: dark) { :host {
  color-scheme: dark;
  --bg: #201B20;
  --bg-sunken: #19151A;
  --surface: #2A2429;
  --surface-2: #30282F;
  --surface-3: #382E36;
  --surface-hover: #3B2D36;
  --surface-active: #493039;
  --border: #51404B;
  --border-strong: #806571;
  --divider: #40323C;
  --text: #F5E8ED;
  --text-2: #E0C7D1;
  --text-3: #C7ABB8;
  --text-disabled: #AB909E;
  --brand: #F0CDD6;
  --brand-hover: #F6DCE3;
  --brand-press: #E7BBC9;
  --brand-soft: #493039;
  --brand-soft-2: #583846;
  --brand-text: #F0B9CB;
  --brand-on: #3C2D32;
  --brand-border: #D7A3B7;
  --focus: #F0B9CB;
  --stage-start: #493039;
  --stage-end: #382B35;
  --stage-text: #F5E8ED;
  --stage-muted: #D9BFCB;
  --ok: #9CC5AF;
  --ok-soft: #253E33;
  --ok-text: #B4D8C5;
  --warn: #E9BC7A;
  --warn-soft: #493820;
  --warn-text: #F0CC99;
  --danger: #F298AA;
  --danger-soft: #4B2834;
  --danger-text: #FAB0BF;
  --info: #ADC2E8;
  --info-soft: #2D384D;
  --info-text: #C0D1F0;
  --ai: #C7ACDC;
  --ai-soft: #3D304C;
  --ai-text: #DAC5E9;
  --live: #F298AA;
  --live-soft: #4B2834;
  --studio-accent: #F0B6CC;
  --danger-on: #3C2D32;
  --live-on: #3C2D32;
  --warn-on: #3C2D32;
  --info-on: #3C2D32;
  --ai-on: #3C2D32;
  --ink: var(--text);
  --muted: var(--text-3);
  --line: var(--border);
  --soft: var(--brand-soft);
} }
/* SAKURA GENERATED END */
:host{all:initial}*{box-sizing:border-box}#box{position:fixed;left:16px;bottom:16px;z-index:2147483647;width:304px;max-width:calc(100vw - 32px);font:12px/1.5 "Segoe UI",sans-serif;color:var(--ink);background:var(--surface);border:1px solid var(--line);border-radius:16px;box-shadow:0 8px 32px #18253620;overflow:hidden}
      #bar{padding:14px 16px;background:linear-gradient(135deg,var(--stage-start),var(--stage-end))}#head{display:flex;align-items:center;gap:7px}#head .wordmark{display:flex;align-items:baseline;gap:2px;line-height:1}#head .wordmark small{font-size:6px;font-weight:600}#head .wordmark b{font-size:24px;letter-spacing:-.04em;font-weight:750}#api{font-size:10px;color:var(--muted);margin-left:3px}#clock{margin-left:auto;font-size:17px;font-weight:650;font-variant-numeric:tabular-nums}#dot{width:6px;height:6px;border-radius:50%;background:#9aa6b6}#dot.running{background:var(--ok);box-shadow:0 0 0 3px var(--ok-soft)}
      #product{font-size:13px;font-weight:600;margin:10px 0 6px;overflow-wrap:anywhere;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}#vsync{font-size:11px;color:var(--muted)}#ai,#collect,#status{font-size:11px;overflow-wrap:anywhere;margin-top:7px}#status{color:var(--muted)}.bad{color:var(--danger-text)!important}.off{color:var(--muted)}.on{color:var(--brand-text)}
      #detail{display:none;padding:0 16px 14px}#detail.open{display:block}#footer{display:flex;justify-content:space-between;align-items:center;border-top:1px solid var(--divider);padding:7px 12px;background:var(--surface-2)}button,a{transition:background 160ms ease,color 160ms ease,transform 120ms ease;font:600 11px "Segoe UI",sans-serif;color:var(--brand-text);border:0;background:none;text-decoration:none;padding:5px;cursor:pointer;border-radius:5px}button:hover,a:hover{background:var(--brand-soft)}button:focus-visible,a:focus-visible{outline:2px solid var(--focus);outline-offset:2px}#detail.open{animation:reveal 160ms ease-out}@keyframes reveal{from{opacity:0;transform:translateY(3px)}to{opacity:1;transform:translateY(0)}}@media(prefers-reduced-motion:reduce){*,*::before,*::after{animation:none!important;transition:none!important}}
      </style><div id="box"><div id="bar"><div id="head"><span id="dot"></span><strong class="wordmark" aria-label="Kid TE"><small>K</small><b>T</b><small>i</small><b>E</b><small>d</small></strong><span id="api">${platform === 'shopee' ? 'Shopee' : 'TikTok'} LIVE</span><span id="clock">—</span></div><div id="product">Chờ xác nhận sản phẩm</div><div id="vsync">Chờ nguồn phát</div></div><div id="detail"><div id="ai">AI · chờ cập nhật</div><div id="collect"></div><div id="status" role="status">Đang kết nối…</div></div><div id="footer"><button id="expand" aria-expanded="false" aria-controls="detail">Chi tiết phiên</button><a href="http://127.0.0.1:8793/#overview" target="_blank" rel="noopener">Mở Kid TE ↗</a></div></div>`;
      root.querySelector('#expand').addEventListener('click', () => {
        const open = root.querySelector('#detail').classList.toggle('open');
        root.querySelector('#expand').setAttribute('aria-expanded', String(open));
      });
      document.documentElement.appendChild(host);
      return host;
    },
    update(host, value) {
      const root = host.shadowRoot;
      const seconds = Number(value.positionS);
      root.querySelector('#clock').textContent = value.videoTitle && Number.isFinite(seconds) ? `${Math.floor(seconds/60)}:${String(Math.floor(seconds)%60).padStart(2,'0')}` : '—';
      root.querySelector('#product').textContent = value.productTitle || 'Chưa xác nhận sản phẩm';
      root.querySelector('#vsync').textContent = value.videoTitle ? `OBS · ${value.videoTitle}` : 'OBS · chờ nguồn phát';
      root.querySelector('#status').textContent = value.error || (value.confirmed ? 'Đã xác nhận sản phẩm trên LIVE' : value.label || 'Đang xác minh sản phẩm');
      root.querySelector('#status').classList.toggle('bad', value.ok === false);
      root.querySelector('#dot').classList.toggle('running', value.ok === true && value.confirmed === true);
      root.querySelector('#ai').textContent = 'AI · xem trạng thái trong tiện ích Shopee';
    },
  };
})();
