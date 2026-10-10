"use strict";
(() => {
  async function sendTurn(turn, adapter) {
    if (turn.protocol_version !== 2 || !Array.isArray(turn.messages) ||
        turn.messages.length < 1 || turn.messages.length > 3) throw new Error('Giao thức tin không hợp lệ');
    const wait = adapter.wait || (ms => new Promise(resolve => setTimeout(resolve, ms)));
    let sent = 0;
    for (const original of turn.messages) {
      const identity = {turn_id: turn.turn_id, room_id: turn.room_id,
        message_id: original.message_id, content_hash: original.content_hash};
      let lease;
      for (;;) {
        if (!await adapter.active()) throw new Error('AI hoặc phòng LIVE đã dừng');
        const status = await adapter.control('status', identity);
        if (!status?.ok) throw new Error(status?.error || 'Không đọc được trạng thái tin');
        const message = status.turn.messages.find(row => row.message_id === original.message_id);
        if (!message || message.content !== original.content || message.content_hash !== original.content_hash) {
          throw new Error('Nội dung tin đã thay đổi');
        }
        if (message.state === 'sent') { sent++; break; }
        if (['unknown', 'failed', 'expired', 'cancelled'].includes(message.state)) {
          throw new Error(`Tin ${message.ordinal + 1}: ${message.state} · cần đối soát`);
        }
        lease = await adapter.control('claim', identity);
        if (lease?.ok) break;
        // Facts changed before the first send (B06): stop, send nothing, report no unknown
        // receipt. The backend recomposes at most once and serves it on the next request.
        if (['revision_changed', 'revision_stale'].includes(lease?.error)) {
          return {ok: false, stopped: lease.error, sent};
        }
        if (!['delivery_cooldown', 'delivery_platform_busy'].includes(lease?.error)) {
          throw new Error(lease?.error || 'Không có quyền gửi');
        }
        await wait(Math.min(1000, Math.max(100, Number(lease.retry_after_s || .5) * 1000)));
      }
      if (!lease) continue; // Receipt already confirmed; never repeat the API call.
      if (lease.content !== original.content || lease.content_hash !== original.content_hash) {
        throw new Error('Claim không khớp nội dung tin');
      }
      if (!await adapter.active()) throw new Error('AI hoặc phòng LIVE đã dừng');
      const dispatched = await adapter.control('dispatched', {...identity, claim_token: lease.claim_token});
      if (!dispatched?.ok) throw new Error(dispatched?.error || 'Lease gửi đã hết hạn');
      let receipt;
      try {
        receipt = await adapter.send(original, identity);
        if (!receipt?.ok) throw new Error(receipt?.error || 'Kết quả gửi chưa rõ');
      } catch (error) {
        await adapter.unknown(identity).catch(() => {});
        throw error;
      }
        const proof = await adapter.ack({...identity, receipt: {
          room_id: turn.room_id, response_code: receipt.response_code,
          ...(receipt.message_id ? {shopee_message_id: String(receipt.message_id)} : {}),
          sent_at_ms: receipt.sent_at_ms
        }});
        if (!proof?.ok) throw new Error('Đã gửi; receipt đang chờ đồng bộ, không gửi lại');
        if (adapter.onSent) await adapter.onSent(original, receipt);
        sent++;
    }
    return {ok: true, sent};
  }
  const api = {sendTurn};
  globalThis.KidTEMessageDelivery = api;
  if (typeof module !== 'undefined') module.exports = api;
})();
