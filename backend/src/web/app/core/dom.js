// Tiện ích DOM tối giản, không phụ thuộc thư viện.
// h('div', {class: 'x', onClick: fn, dataset: {id: 1}}, child1, [child2, child3])

const SVG_NS = 'http://www.w3.org/2000/svg';
const BOOL_PROPS = new Set(['checked', 'disabled', 'selected', 'hidden', 'readOnly', 'multiple', 'autofocus', 'open']);

function applyProps(node, props) {
  if (!props) return;
  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined || value === false && !BOOL_PROPS.has(key)) continue;
    if (key === 'class' || key === 'className') node.setAttribute('class', Array.isArray(value) ? value.filter(Boolean).join(' ') : String(value));
    else if (key === 'text') node.textContent = String(value);
    else if (key === 'style' && typeof value === 'object') {
      for (const [k, v] of Object.entries(value)) { if (k.startsWith('--')) node.style.setProperty(k, String(v)); else node.style[k] = v; }
    }
    else if (key === 'dataset') Object.entries(value).forEach(([k, v]) => { if (v !== undefined && v !== null) node.dataset[k] = String(v); });
    else if (key.startsWith('on') && typeof value === 'function') node.addEventListener(key.slice(2).toLowerCase(), value);
    else if (key === 'ref' && typeof value === 'function') value(node);
    else if (BOOL_PROPS.has(key)) node[key] = Boolean(value);
    else if (key === 'value' && 'value' in node && !(node instanceof SVGElement)) node.value = String(value);
    else node.setAttribute(key, value === true ? '' : String(value));
  }
}

function appendChildren(node, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false || child === true) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

export function h(tag, props, ...children) {
  const node = document.createElement(tag);
  if (props instanceof Node || typeof props === 'string' || Array.isArray(props)) { children.unshift(props); props = null; }
  applyProps(node, props);
  appendChildren(node, children);
  return node;
}

export function s(tag, props, ...children) {
  const node = document.createElementNS(SVG_NS, tag);
  applyProps(node, props);
  appendChildren(node, children);
  return node;
}

export function clear(node, ...children) {
  node.replaceChildren();
  appendChildren(node, children);
  return node;
}

export function debounce(fn, wait = 200, owner = null) {
  let timer = 0;
  const wrapped = (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), wait); };
  wrapped.cancel = () => clearTimeout(timer);
  if (owner && owner.add) owner.add(wrapped.cancel);
  return wrapped;
}

export function rafThrottle(fn) {
  let frame = 0, lastArgs = null;
  return (...args) => {
    lastArgs = args;
    if (frame) return;
    frame = requestAnimationFrame(() => { frame = 0; fn(...lastArgs); });
  };
}

let uidCounter = 0;
export function uid(prefix = 'u') { uidCounter += 1; return prefix + Date.now().toString(36) + uidCounter.toString(36); }

export function isTypingTarget(target) {
  if (!target || !(target instanceof Element)) return false;
  if (target.isContentEditable) return true;
  const tag = target.tagName;
  if (tag === 'TEXTAREA' || tag === 'SELECT') return true;
  if (tag === 'INPUT') return !['checkbox', 'radio', 'range', 'button', 'submit'].includes(target.type);
  return false;
}

/** Gom listener để gỡ sạch khi rời trang (chống rò rỉ khi mount/unmount). */
export function disposer() {
  const fns = [];
  let closed = false;
  const add = fn => { if (closed) { fn(); } else { fns.push(fn); } return fn; };
  return {
    on(target, type, fn, opts) { if (!closed) { target.addEventListener(type, fn, opts); add(() => target.removeEventListener(type, fn, opts)); } return fn; },
    add,
    interval(fn, ms) { if (closed) return 0; const id = setInterval(fn, ms); add(() => clearInterval(id)); return id; },
    timeout(fn, ms) { if (closed) return 0; const id = setTimeout(fn, ms); add(() => clearTimeout(id)); return id; },
    dispose() { if (closed) return; closed = true; while (fns.length) { try { fns.pop()(); } catch (_) { /* bỏ qua */ } } },
  };
}
