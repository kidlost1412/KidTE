// Khu Video: #video (thư viện) · #video/studio/<video_id> · #video/review/<job_id>
import {mountLibrary} from './library.js';
import {mountStudio} from './studio.js';

export default {
  mount(ctx) {
    const {sub, id} = ctx.route;
    const host = ctx.shell.main;
    if ((sub === 'studio' || sub === 'review') && id) {
      return mountStudio(ctx, host, {kind: sub === 'review' ? 'review' : 'library', id});
    }
    return mountLibrary(ctx, host, {autoUpload: id === 'upload'});
  },
};
