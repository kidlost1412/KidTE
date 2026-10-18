import {h, clear, disposer} from '../core/dom.js';
import {pageHead} from '../ui/kit.js';
import {salesPlaybookCard} from './sales-playbook-ui.js';

export default {
  mount(ctx) {
    const d = disposer();
    const book = salesPlaybookCard();
    const host = h('div', {style: {marginTop: '16px'}}, book.node);
    clear(ctx.shell.main, h('div', {class: 'page'},
      pageHead('Sổ tay tình huống', 'Cách AI xử lý câu khó. Bấm vào một tình huống để xem và sửa.'), host));
    return {unmount: () => d.dispose(), isDirty: book.isDirty};
  },
};
