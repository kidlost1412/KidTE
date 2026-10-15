import json
import sys
import os
import tempfile
import threading
import types
import unittest
import urllib.error
import urllib.request
from unittest import mock
from http.server import ThreadingHTTPServer
from pathlib import Path


SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from customer_memory import CustomerMemory  # noqa: E402
from knowledge import KnowledgeBase, select_live_product  # noqa: E402
from server import (  # noqa: E402
    Handler, LiveCatalogStore, LoopbackHTTPServer, VideoPositionStore,
    MissingReporter, RawStreamRecorder, ReplyEngine,
    customer_price_signal, denies_catalog, price_context,
    verified_dose_calculations,
    violates_price_policy,
)
import server as server_module  # noqa: E402
from processor_loader import load_processor  # noqa: E402
from config import SYSTEM_PROMPT_PATH  # noqa: E402


class LoopbackServerTests(unittest.TestCase):
    def test_runtime_server_disables_port_reuse(self):
        self.assertFalse(LoopbackHTTPServer.allow_reuse_address)


class FakeResponder:
    def __init__(self):
        self.calls = []

    def reply(self, nickname, comment, context, max_chars, **kwargs):
        self.calls.append((nickname, comment, context, max_chars))
        return {"action": "reply", "reply": "Dạ, câu trả lời đủ dữ kiện ạ."}


class UnavailableResponder(FakeResponder):
    def check_balance(self):
        return {
            "ok": False,
            "reason": "deepseek_insufficient_balance",
            "error": "DeepSeek đã hết số dư; AI trả lời vẫn tắt",
        }


def live_product(original_price="120.000₫"):
    return {
        "room_id": "room-1", "pin_id": "p1",
        "products": [{
            "product_id": "p1", "title": "Kích Rễ Giâm Cành TC01",
            "original_price": original_price, "sale_price": "90.000₫",
            "promotion": "Mua 1 tặng 1", "skus": [{
                "sku_id": "s1", "title": "Chai 100ml",
                "original_price": original_price,
            }],
        }],
        "raw_api_data": {"products": [{"product_id": "p1"}]},
    }


def comment(message="giá bao nhiêu", cid="c1", uid="u1"):
    return {
        "comment": message, "comment_id": cid, "user_id": uid,
        "unique_id": f"user{uid}", "nickname": f"Khách {uid}",
        "live_context": live_product(),
    }


class DenyingResponder(FakeResponder):
    """Tái hiện đúng hai câu đã làm mất đơn trong LIVE 10/09."""

    def __init__(self, bad_reply, good_reply="Vậy là tài khoản anh có mã rồi đó, chốt liền nha."):
        super().__init__()
        self.bad_reply = bad_reply
        self.good_reply = good_reply

    def reply(self, nickname, comment, context, max_chars, correction="", **kwargs):
        self.calls.append((nickname, comment, context, max_chars))
        if correction:
            return {"action": "reply", "reply": self.good_reply}
        return {"action": "reply", "reply": self.bad_reply}


class StubbornResponder(DenyingResponder):
    def reply(self, nickname, comment, context, max_chars, correction="", **kwargs):
        self.calls.append((nickname, comment, context, max_chars))
        return {"action": "reply", "reply": self.bad_reply}


class StandaloneServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self._catalog_path = server_module.LIVE_CATALOG_PATH
        server_module.LIVE_CATALOG_PATH = root / "live_products_current.json"
        self.catalog = LiveCatalogStore()
        self.catalog.snapshot = {}
        self.catalog.sync = self._sync
        self.reporter = MissingReporter()
        self.reporter.path = root / "missing.jsonl"
        memory = CustomerMemory(
            root / "memory.sqlite3", root / "journals",
            session_id="session-1", max_prior_turns=5,
        )
        catalog_data = {
            "nhom_san_pham": [{"san_pham": [{
                "san_pham_id": "SP-03", "ten_san_pham": "Kích Rễ Giâm Cành TC01",
                "approved_for_live": True,
                "shop_product_ids": ["p1"],
                "excluded_shop_product_ids": ["p-khac-dong"],
                "listing_aliases": "TC01 Kích Rễ Cực Mạnh",
                "huong_dan_su_dung": (
                    "Cây mới trồng: sau khoảng 7–10 ngày có thể bắt đầu phun; "
                    "pha 10ml cho bình 16 lít"
                ),
                "cong_dung": "Hỗ trợ kích rễ",
                "live_promotion": "Mua 1 tặng 1",
            }, {
                "san_pham_id": "SP-07", "ten_san_pham": "Nano Tonic",
                "approved_for_live": True,
                "shop_product_ids": ["p2"],
                "listing_aliases": "Nano Tonic 250ml",
                "cong_dung": "Hỗ trợ phòng nấm rễ và tuyến trùng",
                "doi_tuong_cay_trong": "Cây ăn trái, cây công nghiệp",
                "huong_dan_su_dung": "Dùng đúng hướng dẫn trên nhãn",
            }]}]
        }
        catalog_path = root / "knowledge.json"
        profiles_path = root / "profiles.json"
        catalog_path.write_text(json.dumps(catalog_data, ensure_ascii=False), encoding="utf-8")
        profiles_path.write_text('{"products":{}}', encoding="utf-8")
        self.responder = FakeResponder()
        self.engine = ReplyEngine(
            responder=self.responder, memory=memory,
            knowledge=KnowledgeBase(catalog_path, profiles_path),
            catalog_store=self.catalog, missing_reporter=self.reporter,
        )

    def _sync(self, snapshot):
        self.catalog.snapshot = {
            "room_id": snapshot.get("room_id", ""),
            "pin_id": snapshot.get("pin_id", ""),
            "products": snapshot.get("products", []),
            "product_count": len(snapshot.get("products", [])),
            "synced_at": "now",
        }
        return self.catalog.snapshot

    def tearDown(self):
        server_module.LIVE_CATALOG_PATH = self._catalog_path
        self.engine.close()
        self.temp.cleanup()

    def test_only_original_price_is_quotable_and_sale_price_is_hidden(self):
        result = self.engine.handle(comment())
        self.assertTrue(result["should_send"])
        context = self.responder.calls[-1][2]
        self.assertEqual(context["price"]["quotable_price"], "120.000₫")
        self.assertEqual(context["price"]["quotable_price_source"], "original_price")
        # Giá sale tuyệt đối không được lọt vào context để model khỏi nói ra.
        blob = json.dumps(context, ensure_ascii=False)
        self.assertNotIn("90.000₫", blob)
        self.assertNotIn("sale_price", blob)
        self.assertIn("mã TikTok", context["price_policy"])

    def test_room_host_from_live_context_is_not_answered_and_heartbeat_exposes_it(self):
        host = {'user_id': 'h77', 'unique_id': 'Shop_ABC', 'source': 'room_info'}
        mine = comment('Xin chào cả nhà', 'host-c1', uid='h77')
        mine['live_context'] = {**mine['live_context'], 'host': host}
        self.assertEqual(self.engine.handle(mine)['reason'], 'host_comment')
        other = comment('Xin chào cả nhà', 'host-c2', uid='x1')
        other['unique_id'] = 'shop_abc'  # khác user_id nhưng trùng unique_id (không phân biệt hoa/thường)
        other['live_context'] = {**other['live_context'], 'host': host}
        self.assertEqual(self.engine.handle(other)['reason'], 'host_comment')
        guest = comment('TC01 dùng sao', 'host-c3', uid='k9')
        guest['live_context'] = {**guest['live_context'], 'host': host}
        self.assertNotEqual(self.engine.handle(guest).get('reason'), 'host_comment')
        self.engine.heartbeat({'platform': 'tiktok', 'session_id': 'room-1', 'host': {**host, 'junk': 'x'}})
        self.assertEqual(self.engine.live_channels()['tiktok']['extension']['host']['unique_id'], 'Shop_ABC')
        self.assertNotIn('junk', self.engine.platform_heartbeats['tiktok']['host'])

    def test_empty_heartbeat_from_other_tab_does_not_override_controller(self):
        hb = self.engine.platform_heartbeats
        self.engine.heartbeat({'platform': 'tiktok', 'session_id': 'room-1', 'ai_ready': True,
                               'ai_enabled': True, 'live_running': True, 'tab_id': 'tab-ctrl'})
        result = self.engine.heartbeat({'platform': 'tiktok', 'session_id': '', 'ai_ready': False,
                                        'tab_id': 'tab-other'})
        self.assertEqual(result.get('ignored'), 'not_controller')
        self.assertEqual(hb['tiktok']['session_id'], 'room-1')
        self.assertEqual(self.engine.extension_heartbeat['tab_id'], 'tab-ctrl')
        # Cùng tab tắt AI: cập nhật ngay.
        self.engine.heartbeat({'platform': 'tiktok', 'session_id': 'room-1', 'ai_ready': False,
                               'ai_enabled': False, 'tab_id': 'tab-ctrl'})
        self.assertFalse(hb['tiktok']['ai_enabled'])
        self.assertFalse(hb['tiktok']['ai_ready'])
        # Heartbeat cũ đã quá hạn: tab khác được ghi đè.
        self.engine.heartbeat({'platform': 'tiktok', 'session_id': 'room-1', 'ai_ready': True,
                               'tab_id': 'tab-ctrl'})
        hb['tiktok']['received_at'] -= 31
        self.engine.heartbeat({'platform': 'tiktok', 'session_id': '', 'tab_id': 'tab-other'})
        self.assertEqual(hb['tiktok']['tab_id'], 'tab-other')

    def test_message_claim_requires_current_enabled_room_and_receipt_hash(self):
        answer = self.engine.handle(comment('TC01 dùng sao', 'claim-api'))
        turn = self.engine.delivery_store.get(answer['reply_id'])
        message = turn['messages'][0]
        payload = {'turn_id': turn['turn_id'], 'room_id': 'room-1', 'message_id': message['message_id']}
        self.assertEqual(self.engine.delivery_operation('tiktok', 'claim', payload)['error'], 'delivery_control_inactive')
        self.engine.heartbeat({'platform': 'tiktok', 'session_id': 'room-1',
            'live_running': True, 'ai_enabled': True, 'ai_ready': True})
        claim = self.engine.delivery_operation('tiktok', 'claim', payload)
        self.assertTrue(claim['ok'])
        self.engine.heartbeat({'platform': 'tiktok', 'session_id': 'room-1', 'ai_enabled': False})
        dispatch = {**payload, 'claim_token': claim['claim_token']}
        self.assertFalse(self.engine.delivery_operation('tiktok', 'dispatched', dispatch)['ok'])
        self.engine.heartbeat({'platform': 'tiktok', 'session_id': 'room-1',
            'live_running': True, 'ai_enabled': True, 'ai_ready': True})
        self.assertTrue(self.engine.delivery_operation('tiktok', 'dispatched', dispatch)['ok'])
        proof = {**payload, 'content_hash': message['content_hash'],
                 'receipt': {'room_id': 'room-1', 'response_code': 0}}
        self.assertFalse(self.engine.delivery_operation('tiktok', 'receipt', {**proof, 'content_hash': 'wrong'})['ok'])
        self.assertTrue(self.engine.delivery_operation('tiktok', 'receipt', proof)['ok'])

    def test_v2_three_messages_and_partial_history_only_contains_confirmed_content(self):
        self.responder.reply = lambda *_args, **_kwargs: {'action': 'reply',
            'reply': 'Tin đầu. CHUA_GUI_2 CHUA_GUI_3', 'messages': ['Tin đầu.', 'CHUA_GUI_2', 'CHUA_GUI_3']}
        answer = self.engine.handle({**comment('TC01 dùng sao', 'multi'), 'protocol_version': 2})
        self.assertEqual(len(answer['messages']), 3)
        self.assertEqual(answer['protocol_version'], 2)
        legacy = self.engine.acknowledge_reply({'reply_id':answer['turn_id'], 'interaction_id':answer['interaction_id'],
            'receipt':{'room_id':'room-1','response_code':0}})
        self.assertFalse(legacy['confirmed'])
        self.engine.heartbeat({'platform': 'tiktok', 'session_id': 'room-1',
            'live_running': True, 'ai_enabled': True, 'ai_ready': True})
        message = answer['messages'][0]
        payload = {'turn_id': answer['turn_id'], 'room_id': 'room-1', 'message_id': message['message_id']}
        claim = self.engine.delivery_operation('tiktok', 'claim', payload)
        self.assertTrue(self.engine.delivery_operation('tiktok', 'dispatched', {**payload, 'claim_token': claim['claim_token']})['ok'])
        self.assertTrue(self.engine.delivery_operation('tiktok', 'receipt', {**payload,
            'content_hash': message['content_hash'], 'receipt': {'room_id': 'room-1', 'response_code': 0}})['ok'])
        self.assertEqual(self.engine.delivery_store.get(answer['turn_id'])['state'], 'partially_sent')
        history = self.engine.memory.context_for_reply(answer['customer_key'], 'next', 'Mai')
        self.assertIn('Tin đầu.', history)
        self.assertNotIn('CHUA_GUI', history)
        import time
        now = [time.time()+3]
        self.engine.delivery_store.clock = lambda: now[0]
        for next_message in answer['messages'][1:]:
            next_payload = {**payload, 'message_id':next_message['message_id']}
            next_claim = self.engine.delivery_operation('tiktok','claim',next_payload)
            self.assertTrue(next_claim['ok'])
            self.assertTrue(self.engine.delivery_operation('tiktok','dispatched',
                {**next_payload,'claim_token':next_claim['claim_token']})['ok'])
            self.assertTrue(self.engine.delivery_operation('tiktok','receipt',{**next_payload,
                'content_hash':next_message['content_hash'], 'receipt':{'room_id':'room-1','response_code':0,
                'sent_at_ms':now[0]*1000}})['ok'])
            now[0]+=3
        self.assertTrue(self.engine.delivery_operation('tiktok', 'receipt', {**payload,
            'content_hash':message['content_hash'],'receipt':{'room_id':'room-1','response_code':0}})['ok'])

    def test_v2_queue_restores_prepared_turn_without_model_regeneration(self):
        self.engine.heartbeat({'platform':'tiktok','session_id':'room-1',
            'live_running':True,'ai_enabled':True,'ai_ready':True})
        payload = {**comment('TC01 dùng sao','recover-v2'),'protocol_version':2}
        first = self.engine.queued_reply('tiktok',payload)
        self.assertEqual(first['protocol_version'],2)
        count = len(self.responder.calls)
        restored = self.engine.queued_reply('tiktok',payload)
        self.assertEqual(restored['turn_id'], first['turn_id'])
        self.assertEqual(len(self.responder.calls),count)
        self.assertEqual(restored['messages'],first['messages'])

    def test_queue_api_serializes_generation_until_delivery_settled(self):
        self.engine.heartbeat({'platform': 'tiktok', 'session_id': 'room-1',
            'live_running': True, 'ai_enabled': True, 'ai_ready': True})
        for comment_id in ('q1', 'q2'):
            row = self.engine.queue_operation('tiktok', 'enqueue', {'room_id': 'room-1',
                'comment_payload': comment('TC01 dùng sao', comment_id)})
            self.assertEqual(row['state'], 'queued')
        first = self.engine.queue_operation('tiktok', 'next', {'room_id': 'room-1'})
        self.assertTrue(first['result']['should_send'])
        recovered = self.engine.queue_operation('tiktok', 'next', {'room_id': 'room-1'})
        self.assertTrue(recovered['recovered'])
        self.assertEqual(recovered['result']['reply_id'], first['result']['reply_id'])
        self.engine.comment_queue.settle(first['result']['reply_id'], 'sent')
        self.assertTrue(self.engine.queue_operation('tiktok', 'next', {'room_id': 'room-1'})['available'])

    def test_queue_cancel_endpoint_cancels_queued_item_only(self):
        self.engine.queue_operation('tiktok', 'enqueue', {'room_id': 'room-1',
            'comment_payload': comment('TC01 dùng sao', 'c1')})
        self.assertEqual(self.engine.queue_operation('tiktok', 'cancel',
            {'room_id': 'room-1', 'comment_id': 'c1'}), {'ok': True, 'cancelled': True})
        self.assertEqual(self.engine.queue_operation('tiktok', 'cancel',
            {'room_id': 'room-1', 'comment_id': 'c1'})['cancelled'], False)

    def test_queue_batch_enqueue_is_fifo_with_per_item_results_and_waiting_state(self):
        self.engine.heartbeat({'platform': 'tiktok', 'session_id': 'room-1',
            'live_running': True, 'ai_enabled': True, 'ai_ready': True})
        batch = [comment('TC01 dùng sao', 'b1'), {'comment': 'x'}, comment('TC01 dùng sao', 'b2')]
        out = self.engine.queue_operation('tiktok', 'enqueue', {'room_id': 'room-1', 'comment_payloads': batch})
        self.assertEqual([r['ok'] for r in out['results']], [True, False, True])
        self.assertEqual(out['results'][1]['error'], 'queue_customer_invalid')
        again = self.engine.queue_operation('tiktok', 'enqueue', {'room_id': 'room-1', 'comment_payloads': batch[:1]})
        self.assertEqual(again['results'][0]['queue_id'], out['results'][0]['queue_id'])
        first = self.engine.queue_operation('tiktok', 'next', {'room_id': 'room-1'})
        self.assertEqual(first['queue_id'], out['results'][0]['queue_id'])
        waiting = self.engine.queued_reply('tiktok', {**comment('TC01 dùng sao', 'b2'), 'protocol_version': 2})
        self.assertEqual(waiting['reason'], 'queue_waiting')
        self.assertEqual(waiting['queue_id'], out['results'][2]['queue_id'])
        self.assertEqual(waiting['state'], 'queued')
        with self.assertRaises(ValueError):
            self.engine.queue_operation('tiktok', 'enqueue', {'room_id': 'room-1',
                'comment_payloads': [comment('a', str(i)) for i in range(51)]})

    def test_tiktok_receipt_survives_backend_restart(self):
        answer = self.engine.handle(comment('TC01 dùng sao', 'restart-receipt'))
        path = self.engine.memory.db_path
        knowledge = self.engine.knowledge
        self.engine.close()
        self.engine = ReplyEngine(responder=self.responder,
            memory=CustomerMemory(path, Path(self.temp.name)/'journals', session_id='session-2'),
            knowledge=knowledge, catalog_store=self.catalog, missing_reporter=self.reporter)
        payload = {'interaction_id': answer['interaction_id'], 'reply_id': answer['reply_id'],
                   'receipt': {'room_id': 'room-1', 'response_code': 0}}
        self.assertTrue(self.engine.acknowledge_reply(payload)['confirmed'])
        self.assertTrue(self.engine.acknowledge_reply(payload)['confirmed'])
        bad = {**payload, 'receipt': {'room_id': 'room-other', 'response_code': 0}}
        self.assertFalse(self.engine.acknowledge_reply(bad)['confirmed'])
        self.assertEqual(self.engine.handle(comment('TC01 dùng sao', 'restart-receipt'))['reason'], 'duplicate')
        other_room = comment('TC01 dùng sao', 'restart-receipt')
        other_room['live_context']['room_id'] = 'room-2'
        self.assertTrue(self.engine.handle(other_room)['should_send'])

    def test_expired_comment_does_not_call_the_model(self):
        import time
        payload = {**comment(), 'received_at_ms': time.time() * 1000 - 121000}
        self.assertEqual(self.engine.handle(payload)['reason'], 'expired')
        self.assertEqual(self.responder.calls, [])

    def test_declared_price_in_the_approved_profile_wins(self):
        self.engine.knowledge.records[0]["gia_goc_duyet"] = "620.000₫"
        payload = comment("giá bao nhiêu", "declared-price")
        payload["live_context"] = live_product(original_price="")
        result = self.engine.handle(payload)
        self.assertTrue(result["should_send"])
        price = self.responder.calls[-1][2]["price"]
        self.assertEqual(price["quotable_price"], "620.000₫")
        self.assertEqual(price["quotable_price_source"], "gia_goc_da_duyet")

    def test_missing_original_price_never_falls_back_to_sale_price(self):
        """API không trả original_price mà chỉ còn sale_price nghĩa là có một
        đợt giảm giá đang chạy nhưng không lộ được giá gốc — đây CHÍNH LÀ tình
        huống nguy hiểm nhất, không phải trường hợp vô hại để đọc tạm
        sale_price. Phải coi như không có dữ liệu, không được đoán."""
        payload = comment("giá bao nhiêu", "no-origin")
        payload["live_context"] = live_product(original_price="")
        result = self.engine.handle(payload)
        self.assertTrue(result["should_send"])
        price = self.responder.calls[-1][2]["price"]
        self.assertNotIn("quotable_price", price)
        blob = json.dumps(price, ensure_ascii=False)
        self.assertNotIn("90.000₫", blob)

    def test_customer_lower_price_is_a_personalized_price_signal(self):
        payload = comment("Giá đang 85k", "price-signal")
        result = self.engine.handle(payload)
        self.assertTrue(result["should_send"])
        signal = self.responder.calls[-1][2]["customer_price_signal"]
        self.assertEqual(signal["reported_price_vnd"], 85_000)
        self.assertEqual(signal["relation"], "lower_than_shop_visible_price")
        self.assertIn("mã TikTok", signal["interpretation"])

    def test_bare_price_uses_same_customer_price_context(self):
        signal = customer_price_signal(
            "185", {"original_price": "295.000₫", "sale_price": "205.800₫"},
            "[LIVE này] Khách: Giá bao nhiêu\nĐã trả lời: Giá gốc 295k nha",
        )
        self.assertEqual(signal["reported_price_vnd"], 185_000)
        self.assertEqual(signal["relation"], "lower_than_shop_visible_price")

    def test_non_price_number_is_not_forced_into_price_signal(self):
        self.assertEqual(customer_price_signal(
            "bình 16 lít", {"original_price": "295.000₫"}, ""
        ), {})

    def test_verified_video_context_is_compacted_for_model(self):
        payload = comment("loại này dùng cho cây mới trồng được không", "video-1")
        payload["video_context"] = {
            "position_s": "45.126",
            "transcript_excerpt": "Đang nói về bộ rễ yếu và phun qua lá.",
            "current_topic": "TC01 cho cây mới trồng",
            "scripted_product_id": "p1",
            "sync_confidence": "high",
            "reviewed_script_facts": ["TC01 ở đoạn này phun qua lá"],
            "ignored": "không được chuyển tiếp",
        }
        result = self.engine.handle(payload)
        self.assertTrue(result["should_send"])
        video = self.responder.calls[-1][2]["video_context"]
        self.assertEqual(video["position_s"], 45.13)
        self.assertEqual(video["scripted_product_id"], "p1")
        self.assertEqual(video["reviewed_script_facts"], ["TC01 ở đoạn này phun qua lá"])
        self.assertNotIn("ignored", video)

    def test_reply_is_complete_only_after_tiktok_receipt(self):
        result = self.engine.handle(comment(cid="receipt-1"))
        before = self.engine.memory._db.execute(
            "SELECT response_complete, delivery_status FROM interactions WHERE interaction_id = ?",
            (result["interaction_id"],),
        ).fetchone()
        self.assertEqual((before[0], before[1]), (0, "generated"))
        ack = self.engine.acknowledge_reply({
            "interaction_id": result["interaction_id"],
            "reply_id": result["reply_id"],
            "receipt": {"room_id": "room-1", "response_code": 0},
        })
        self.assertTrue(ack["confirmed"])
        after = self.engine.memory._db.execute(
            "SELECT response_complete, delivery_status FROM interactions WHERE interaction_id = ?",
            (result["interaction_id"],),
        ).fetchone()
        self.assertEqual((after[0], after[1]), (1, "sent"))

    def test_failed_or_cancelled_reply_is_not_left_as_generated(self):
        result = self.engine.handle(comment(cid="delivery-failed"))
        updated = self.engine.update_reply_delivery({
            "interaction_id": result["interaction_id"],
            "reply_id": result["reply_id"],
            "status": "failed",
            "detail": "TikTok timeout",
        })
        self.assertTrue(updated["updated"])
        row = self.engine.memory._db.execute(
            "SELECT response_complete, delivery_status FROM interactions WHERE interaction_id = ?",
            (result["interaction_id"],),
        ).fetchone()
        self.assertEqual((row[0], row[1]), (0, "failed"))

    def test_forget_confirmation_requires_no_receipt_ids(self):
        result = self.engine.handle(comment("xóa dữ liệu của tôi", "forget-1"))
        self.assertTrue(result["should_send"])
        self.assertFalse(result["requires_ack"])
        self.assertNotIn("interaction_id", result)
        self.assertNotIn("reply_id", result)

    def test_sent_reply_echo_is_blocked_even_when_host_flag_is_missing(self):
        first = self.engine.handle(comment("loại này dùng sao", "echo-source", "real"))
        self.assertTrue(first["should_send"])
        ack = self.engine.acknowledge_reply({
            "interaction_id": first["interaction_id"],
            "reply_id": first["reply_id"],
            "receipt": {"room_id": "room-1", "response_code": 0},
        })
        self.assertTrue(ack["confirmed"])
        echo = self.engine.handle(comment(
            "@userreal Dạ, câu trả lời đủ dữ kiện ạ.", "echo-return", "host"
        ))
        self.assertFalse(echo["should_send"])
        self.assertEqual(echo["reason"], "self_echo")
        self.assertEqual(len(self.responder.calls), 1)

    def test_missing_mixing_fact_is_a_soft_hint_not_a_gate(self):
        result = self.engine.handle(comment(
            "TC01 có trộn với thuốc sâu không ạ?", "mix-1"
        ))
        self.assertTrue(result["should_send"])
        self.assertEqual(len(self.responder.calls), 1)
        hint = self.responder.calls[0][2].get("kien_thuc_chung", "")
        self.assertIn("pha chung", hint)
        self.assertLess(len(hint), 400)

    def test_unknown_product_recommendation_is_silent(self):
        result = self.engine.handle(comment(
            "Có loại nào phòng nấm rễ và tuyến trùng không?", "recommend-1"
        ))
        self.assertFalse(result["should_send"])
        self.assertIn("san_pham_phu_hop", result["missing_fields"])
        self.assertEqual(self.responder.calls, [])

    def test_recommendation_retrieves_matching_live_product(self):
        payload = comment(
            "Có loại nào phòng nấm rễ và tuyến trùng không?", "recommend-2"
        )
        payload["live_context"]["products"].append({
            "product_id": "p2", "title": "Nano Tonic 250ml",
            "original_price": "250.000₫", "sale_price": "",
            "promotion": "", "skus": [],
        })
        result = self.engine.handle(payload)
        self.assertTrue(result["should_send"])
        context = self.responder.calls[-1][2]
        self.assertEqual(context["current_product"]["product_id"], "p2")
        self.assertIn("nấm rễ", context["approved_product_knowledge"]["cong_dung"])
        # Package A: sản phẩm đang xét (p2) đã có ở current_product/approved_product_knowledge,
        # nên không lặp lại trong relevant_approved_products nữa.
        self.assertNotIn("p2", [row["live_product"]["product_id"]
                                for row in context["relevant_approved_products"]])

    def test_after_planting_timing_uses_approved_start_window(self):
        result = self.engine.handle(comment(
            "Lọ kích rễ trồng bao nhiêu ngày thì dùng được ạ?", "timing-1"
        ))
        self.assertTrue(result["should_send"])
        self.assertEqual(len(self.responder.calls), 1)
        context = self.responder.calls[0][2]
        directions = context["approved_product_knowledge"]["huong_dan_su_dung"]
        self.assertIn("sau khoảng 7–10 ngày", directions)

    def test_isolation_days_are_not_misread_as_after_planting(self):
        intents = self.engine.knowledge.intents("chai này cách ly mấy ngày chị")
        self.assertFalse(intents["after_planting"])
        self.assertTrue(intents["technical"])

    def test_approved_ratio_is_scaled_deterministically(self):
        rows = verified_dose_calculations(
            "bình 16 lít pha bao nhiêu ml",
            {"dose_extrapolation_rule": "nearest_endpoint",
             "huong_dan_su_dung": ["Pha 240ml cho phuy 200 lít nước"]},
        )
        self.assertEqual(rows[0]["calculated_ml"], 19.2)

    def test_price_question_is_skipped_only_when_no_price_exists_at_all(self):
        payload = comment(cid="c2", uid="u2")
        payload["live_context"] = live_product(original_price="")
        for item in payload["live_context"]["products"]:
            item["sale_price"] = ""
            item["price"] = ""
        result = self.engine.handle(payload)
        self.assertFalse(result["should_send"])
        self.assertEqual(result["reason"], "missing_data")
        self.assertIn("gia_goc", result["missing_fields"])
        self.assertEqual(result["missing_count"], 1)
        row = json.loads(self.reporter.path.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(row["action"], "not_sent")

    def test_dose_question_does_not_require_a_price(self):
        payload = comment("bình 16 lít pha bao nhiêu ml chị", "dose-no-price")
        payload["live_context"] = live_product(original_price="")
        result = self.engine.handle(payload)
        self.assertTrue(result["should_send"])
        self.assertEqual(len(self.responder.calls), 1)

    def test_near_name_product_is_not_misidentified(self):
        payload = comment("loại này trị gì", "c3", "u3")
        payload["live_context"] = {
            "room_id": "room-1", "pin_id": "p5",
            "products": [{
                "product_id": "p5",
                "title": "SẠCH SÂU ĐẤT 1KG Công Nghệ Mới",
                "original_price": "286.868₫",
                "sale_price": "232.364₫",
                "skus": [], "promotion": "",
            }],
        }
        before = len(self.responder.calls)
        result = self.engine.handle(payload)
        self.assertNotEqual(result.get("reason"), "missing_data")
        if len(self.responder.calls) > before:
            self.assertIn("kien_thuc_chung", self.responder.calls[-1][2])

    def test_vietnamese_d_stops_soil_product_matching_scc_product(self):
        record = self.engine.knowledge.match("SẠCH SÂU ĐẤT 1KG Công Nghệ")
        self.assertIsNone(record)

    def test_missing_dose_is_a_soft_hint_not_a_gate(self):
        before = len(self.responder.calls)
        payload = comment("pha liều bao nhiêu", "c4", "u4")
        payload["live_context"] = {
            "room_id": "room-1", "pin_id": "px",
            "products": [{
                "product_id": "px", "title": "Sản phẩm chưa có hồ sơ 500ml",
                "original_price": "100.000₫", "sale_price": "",
                "skus": [], "promotion": "",
            }],
        }
        result = self.engine.handle(payload)
        self.assertTrue(result["should_send"])
        self.assertEqual(len(self.responder.calls), before + 1)
        self.assertIn("liều", self.responder.calls[-1][2]["kien_thuc_chung"])

    def test_memory_neng_no_cross_customer(self):
        self.engine.handle(comment("Tôi trồng cà phê", "a1", "a"))
        self.engine.handle(comment("Tôi trồng xoài", "b1", "b"))
        self.engine.handle(comment("loại này dùng sao", "a2", "a"))
        context = self.responder.calls[-1][2]["customer_memory"]
        self.assertIn("Tôi trồng cà phê", context)
        self.assertNotIn("Tôi trồng xoài", context)

    def test_reply_is_fitted_at_a_sentence_end_not_cut_mid_word(self):
        plugin = load_processor(SRC.parent.parent / "plugins" / "model-deepseek" / "processor.py", "model-deepseek")
        text = "Đạm cá can 5L gốc 555k đó anh. Anh coi tài khoản có mã TikTok hông rồi đặt liền nha nha nha"
        fitted = plugin._fit_reply(text, 50)
        self.assertEqual(fitted, "Đạm cá can 5L gốc 555k đó anh.")
        self.assertFalse(plugin._fit_reply("@sai " + "rất tốt " * 30, 45).startswith("@"))
        self.assertEqual(plugin._strip_nickname("Hello Lan nha.", "Lan"), "Hello nha.")

    def test_system_prompt_forbids_robotic_greetings(self):
        prompt = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
        self.assertIn("Không được nói như chatbot", prompt)
        self.assertIn("Không nói “cảm ơn đã ghé”", prompt)
        self.assertIn("Coi người xem là khách quen", prompt)
        self.assertIn("suy luận nông học có căn cứ", prompt)
        self.assertIn("price.quotable_price", prompt)
        self.assertIn("KHÔNG BAO GIỜ nói shop không có", prompt)
        self.assertIn("Đừng tranh luận để bảo vệ catalog", prompt)
        self.assertIn("hành động ẩn sau câu chữ", prompt)
        self.assertIn("TƯ DUY ĐA GÓC NHÌN", prompt)
        self.assertIn("Lời hệ thống cũ", prompt)

    def _engine_with(self, responder):
        self.engine.responder = responder
        return responder

    def test_a_combo_keeps_its_own_profile_when_a_part_excludes_it(self):
        # Hồ sơ Đạm Cá 5L ghi "combo này không phải tôi" trong
        # excluded_shop_product_ids. Trước đây tập loại trừ là TOÀN CỤC nên
        # combo thành vô chủ, dù chính nó có hồ sơ riêng.
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        catalog = root / "kb.json"
        catalog.write_text(json.dumps({"nhom_san_pham": [{"san_pham": [
            {"san_pham_id": "LIVE-dam5l", "ten_san_pham": "Đạm Cá 5L",
             "approved_for_live": True, "shop_product_ids": ["dam5l"],
             "excluded_shop_product_ids": ["combo1"]},
            {"san_pham_id": "LIVE-combo1", "ten_san_pham": "Combo Đạm Cá + Humic",
             "approved_for_live": True, "shop_product_ids": ["combo1"]},
        ]}]}, ensure_ascii=False), encoding="utf-8")
        profiles = root / "profiles.json"
        profiles.write_text(json.dumps({"products": {}}), encoding="utf-8")
        kb = KnowledgeBase(catalog, profiles)
        self.assertEqual(
            (kb.match("", product_id="combo1") or {}).get("san_pham_id"),
            "LIVE-combo1")
        # SKU thật sự vô chủ thì vẫn phải im lặng.
        self.assertIsNone(kb.match("", product_id="khong-ai-nhan"))

    def test_the_word_combo_is_not_a_promotion_question(self):
        # Shop bán 6 SKU tên sẵn là "Combo ...". Coi "combo" là hỏi khuyến
        # mãi thì mọi câu hỏi về combo đều bị chặn vì thiếu chuong_trinh.
        self.assertFalse(KnowledgeBase.intents("combo này pha bình 16 lít sao")["promotion"])
        self.assertTrue(KnowledgeBase.intents("combo này có tặng gì không")["promotion"])
        self.assertTrue(KnowledgeBase.intents("hôm nay có khuyến mãi gì")["promotion"])

    def test_missing_promotion_asks_for_clarification_instead_of_silence(self):
        selected = self.engine._select_product('10 chai kích rễ tặng gì', live_product())
        selected['missing'] = ['chuong_trinh']
        with mock.patch.object(self.engine, '_select_product', return_value=selected):
            result = self.engine.handle(comment('10 chai kích rễ tặng gì', 'promotion-clarification'))
        self.assertTrue(result['should_send'])
        context = self.responder.calls[-1][2]
        self.assertIn('không hỏi lại loại hoặc số lượng đã rõ', context['promotion_status'])

    def test_product_answer_resumes_same_customers_pending_promotion(self):
        self.engine.memory.set_room_id('room-1')
        ref = self.engine.memory.record_comment('10 chai kích rễ tặng gì', comment_id='pending',
            platform_user_id='u1', display_name='Khách u1')
        self.engine.memory.record_reply(ref['interaction_id'], 'Anh hỏi TC01 hay Humic?', complete=True)
        self.engine.memory.note_conversation_state(ref['interaction_id'], {'awaiting_fields': ['product']})
        result = self.engine.handle(comment('tc01', 'clarified'))
        self.assertTrue(result['should_send'])
        self.assertIn('10 chai kích rễ tặng gì', self.responder.calls[-1][1])
        self.assertEqual(self.responder.calls[-1][2]['conversation']['current_message'], 'tc01')
        self.assertIn('promotion', result['metrics']['intents'])
        self.engine.handle(comment('tc01', 'different-customer', uid='u2'))
        self.assertEqual(self.responder.calls[-1][1], 'tc01')
        changed = comment('tc01', 'different-room')
        changed['live_context']['room_id'] = 'room-2'
        self.engine.handle(changed)
        self.assertEqual(self.responder.calls[-1][1], 'tc01')
        room_turns = self.engine.memory.recent_live_turns(ref['customer_key'], '', '', room_id='room-2')
        self.assertEqual([row['comment'] for row in room_turns], ['tc01'])

    def test_wrong_sku_never_borrows_another_products_profile(self):
        payload = comment("chai này pha bao nhiêu ml", "wrong-sku")
        payload["live_context"]["products"][0].update({
            "product_id": "p-khac-dong",
            "title": "VUA BO TRI | Chế Phẩm NO FA RA 350SC Dạng Sữa SC",
        })
        payload["live_context"]["pin_id"] = "p-khac-dong"
        result = self.engine.handle(payload)
        self.assertTrue(result["should_send"])
        self.assertEqual(len(self.responder.calls), 1)
        # Hồ sơ của SKU khác không được mượn: chỉ có gợi ý kiến thức chung, không có liều của SKU kia.
        self.assertIn("kien_thuc_chung", self.responder.calls[0][2])
        self.assertFalse(self.responder.calls[0][2].get("huong_dan_su_dung"))

    def test_price_denial_is_rewritten_before_sending(self):
        responder = self._engine_with(
            DenyingResponder("Giá 120k nha anh, 85k không có.")
        )
        result = self.engine.handle(comment("Giá đang 85k", "deny-price"))
        self.assertTrue(result["should_send"])
        self.assertNotIn("không có", result["reply"])
        self.assertTrue(result["metrics"]["retried"])
        self.assertEqual(len(responder.calls), 2)

    def test_voucher_is_only_asserted_after_the_shopper_reports_one(self):
        # Khách chưa nói gì về giá mà AI đã khẳng định họ có mã là bịa:
        # phải bắt viết lại thành lời mời tự kiểm tra.
        responder = self._engine_with(DenyingResponder(
            "Tài khoản anh đang có mã rồi đó, chốt liền nha.",
            good_reply="Gốc 120k đó anh, nay TikTok hỗ trợ mã lớn lắm, anh xem "
                       "tài khoản có hông rồi đặt liền nha.",
        ))
        result = self.engine.handle(comment("cái này bao nhiêu", "voucher-claim"))
        self.assertTrue(result["should_send"])
        self.assertNotIn("đang có mã", result["reply"])
        self.assertEqual(len(responder.calls), 2)

    def test_pushing_a_sale_at_someone_who_already_bought_is_rewritten(self):
        # Khách đã mua rồi mà AI còn thúc "chốt liền" thì phải bắt viết lại.
        responder = self._engine_with(DenyingResponder(
            "77k là giá sau mã tài khoản anh rồi đó, ngon quá, chốt liền đi nha.",
            good_reply="Dạ giá trên live với mã theo tài khoản đổi theo từng lúc "
                       "anh ơi, anh nhắn shop em coi giúp đơn của anh nha.",
        ))
        result = self.engine.handle(
            comment("vừa mua 1 lọ giá 90k mà giờ xem lại 77k là sao", "da-mua"))
        self.assertTrue(result["should_send"])
        self.assertNotIn("chốt liền", result["reply"])
        self.assertEqual(len(responder.calls), 2)

    def test_price_advisory_does_not_silence_model_reply(self):
        self._engine_with(StubbornResponder("85k không phải giá chai này."))
        result = self.engine.handle(comment("Giá đang 85k", "deny-price-2"))
        self.assertTrue(result["should_send"])
        self.assertEqual(result["reply"], "85k không phải giá chai này.")

    def test_catalog_denial_is_rewritten_before_sending(self):
        responder = self._engine_with(DenyingResponder(
            "Không có thuốc phun kiến nha.",
            "Kiến anh đang gặp ở cây gì để em coi đúng dòng nha?",
        ))
        result = self.engine.handle(
            comment("Nhà mình có thuốc phun kiến không?", "deny-catalog")
        )
        self.assertTrue(result["should_send"])
        self.assertIn("cây gì", result["reply"])
        self.assertEqual(len(responder.calls), 2)

    def _two_products(self, message, pin="p1"):
        payload = comment(message)
        payload["live_context"]["pin_id"] = pin
        payload["live_context"]["products"].append({
            "product_id": "p2", "title": "Nano Tonic 250ml Phòng Nấm Rễ", "original_price": "150.000₫",
        })
        return payload

    def test_learned_alias_selects_the_product_before_the_model(self):
        self.engine.learning.add_item(kind="alias", statement="Khách gọi Bạch Long là Nano Tonic",
                                      product_id="p2", terms=["bạch long"], status="active")
        result = self.engine.handle(self._two_products("bạch long xài sao em"))
        self.assertTrue(result["should_send"])
        context = self.responder.calls[-1][2]
        self.assertEqual(context["current_product"]["product_id"], "p2")
        self.assertEqual(context["kien_thuc_da_hoc"][0]["noi_dung"], "Khách gọi Bạch Long là Nano Tonic")
        self.assertTrue(result["metrics"]["learned_product"])
        # Danh mục cố định không mang cờ ghim/tồn kho (đổi liên tục → vỡ cache).
        self.assertNotIn("is_pinned", context["all_live_products"][0])

    def test_invented_total_price_is_rewritten(self):
        responder = DenyingResponder("2 chai hết 240k nha anh, chốt liền nha.",
                                     good_reply="Gốc 120k một chai đó anh, anh chọn số lượng trong giỏ nha.")
        self.engine.responder = responder
        result = self.engine.handle(comment("lấy 2 chai giá sao", cid="p1"))
        self.assertTrue(result["should_send"])
        self.assertEqual(result["reply"], "Gốc 120k một chai đó anh, anh chọn số lượng trong giỏ nha.")
        self.assertEqual(len(responder.calls), 2)
        live = {"products": [{"original_price": "120.000₫"}]}
        self.assertEqual(self.engine._invented_price({"action": "reply", "reply": "Gốc 120k nha"}, live, {}, {}), "")

    def test_taught_fact_removes_the_general_knowledge_hint(self):
        question = "kích rễ này pha chung với humic được không em"
        soft = self.engine.handle(comment(question, cid="g1"))
        self.assertTrue(soft["should_send"])
        self.assertIn("pha chung", self.responder.calls[-1][2]["kien_thuc_chung"])
        self.engine.learning.add_item(kind="fact", statement="TC01 pha chung được với Humic, pha Humic trước",
                                      product_id="p1", status="active")
        result = self.engine.handle(comment(question, cid="g2"))
        self.assertTrue(result["should_send"])
        self.assertIn("TC01 pha chung được với Humic, pha Humic trước",
                      [k["noi_dung"] for k in self.responder.calls[-1][2]["kien_thuc_da_hoc"]])
        self.assertNotIn("kien_thuc_chung", self.responder.calls[-1][2])
        self.assertIsNone(self.engine.learning.GATE_PATTERNS.get("lieu_dung"))

    def test_suggested_knowledge_is_not_used_until_approved(self):
        self.engine.learning.add_item(kind="alias", statement="Khách gọi Bạch Long là Nano Tonic",
                                      product_id="p2", terms=["bạch long"])
        self.engine.handle(self._two_products("bạch long xài sao em"))
        context = self.responder.calls[-1][2]
        self.assertEqual(context["current_product"]["product_id"], "p1")
        self.assertNotIn("kien_thuc_da_hoc", context)

    def test_alias_keeps_the_pinned_sku_of_the_same_product_line(self):
        a = {"product_id": "a1", "title": "Đạm Cá Amino 1L Chất Hữu Cơ 20%"}
        b = {"product_id": "a5", "title": "Đạm Cá Hữu Cơ Amino (Can 5 Lít)"}
        combo = {"product_id": "c1", "title": "Combo Đạm Cá + Humic 5 Lít"}
        self.assertTrue(ReplyEngine._same_family(a, b))
        self.assertFalse(ReplyEngine._same_family(a, combo))

    def test_try_ai_runs_the_reply_path_without_recording(self):
        self.engine.handle(comment())            # đồng bộ danh mục như lúc LIVE
        count = lambda: self.engine.memory._db.execute("SELECT COUNT(*) FROM interactions").fetchone()[0]
        before_calls, before_rows = len(self.responder.calls), count()
        out = self.engine.learning_try({"comment": "giá bao nhiêu em"})
        self.assertEqual(out["reply"], "Dạ, câu trả lời đủ dữ kiện ạ.")
        self.assertEqual(len(self.responder.calls), before_calls + 1)
        self.assertEqual(count(), before_rows, "Thử AI không được ghi bình luận vào trí nhớ khách")

    def test_catalog_scope_warning_is_always_in_context(self):
        self.engine.handle(comment())
        self.assertIn(
            "KHÔNG CÓ", self.responder.calls[-1][2]["catalog_scope_warning"]
        )

    def test_metrics_are_recorded_for_every_reply(self):
        result = self.engine.handle(comment())
        self.assertIn("total_ms", result["metrics"])
        self.assertIn("price", result["metrics"]["intents"])
        self.assertTrue(result["metrics"]["knowledge_matched"])

    def test_live_extension_source_is_recorded_separately(self):
        payload = comment(cid="source-live")
        payload["data_source"] = "live_extension"
        result = self.engine.handle(payload)
        metadata = self.engine.memory._db.execute(
            "SELECT metadata_json FROM interactions WHERE interaction_id = ?",
            (result["interaction_id"],),
        ).fetchone()[0]
        self.assertEqual(json.loads(metadata)["data_source"], "live_extension")

    def test_health_fails_closed_when_deepseek_is_unavailable(self):
        self.engine.responder = UnavailableResponder()
        health = self.engine.health()
        self.assertFalse(health["ok"])
        self.assertEqual(health["reason"], "deepseek_insufficient_balance")

    def test_health_reports_only_real_plugin_state(self):
        self.engine.plugin_runtime = types.SimpleNamespace(manifests={"dong-bo-video": object()})
        self.engine.plugin_errors = {"dich-vu-mau": "thiếu entry point"}
        health = self.engine.health()
        self.assertEqual(health["plugins"]["loaded"], ["dong-bo-video"])
        self.assertEqual(health["plugins"]["errors"], {"dich-vu-mau": "thiếu entry point"})

    def test_host_echo_marks_unknown_turn_sent_and_settles_queue(self):
        engine = self.engine
        store = engine.delivery_store
        body = '@triaxa150 Dạ pha chung được luôn á bác, Nano đông với TC01 ok nha'
        ref = engine.memory.record_comment('nano đông + tc01 được không chị', comment_id='c1',
            room_id='room-1', platform_user_id='u1', display_name='Vũ style')
        turn = store.prepare('t-echo', 'tiktok', 'room-1', ref['interaction_id'], [body])
        mid = turn['messages'][0]['message_id']
        claim = store.claim('t-echo', mid, 'tiktok', 'room-1')
        self.assertTrue(store.dispatched(mid, claim['claim_token']))
        store._db.execute("UPDATE reply_messages SET state='unknown' WHERE message_id=?", (mid,))
        store._db.commit()
        host = {'user_id': 'h77', 'unique_id': 'Shop_ABC', 'source': 'room_info'}
        echo = comment('@Vũ style 🇻🇳 Dạ pha chung được luôn á bác, Nano đông với TC01 ok nha',
                       'echo-1', uid='h77')
        echo['live_context'] = {**echo['live_context'], 'host': host, 'room_id': 'room-1'}
        self.assertEqual(engine.handle(echo)['reason'], 'host_comment')
        got = store.get('t-echo')
        self.assertEqual(got['state'], 'sent')
        self.assertIn('echo', got['messages'][0]['receipt_json'])


if __name__ == "__main__":
    unittest.main()


class RawStreamTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.recorder = RawStreamRecorder(Path(self.temp.name), enabled=True)

    def tearDown(self):
        self.temp.cleanup()

    def rows(self):
        path = next(Path(self.temp.name).glob("stream_*.jsonl"))
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_host_manual_reply_is_captured_and_paired(self):
        self.recorder.record([
            {"text": "đạm cá bao nhiêu", "user_id": "u9", "unique_id": "bacba"},
            {"text": "@bacba 289k nha anh, mã bên tài khoản anh coi thử",
             "is_host": True, "unique_id": "nongduoctoancauofficial"},
        ])
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertFalse(rows[0]["is_host"])
        self.assertTrue(rows[1]["is_host"])
        self.assertEqual(rows[1]["reply_to_username"], "bacba")

    def test_short_and_ai_off_comments_are_still_captured(self):
        written = self.recorder.record([
            {"text": "ok", "user_id": "u1", "ai_enabled": False},
            {"text": "👍", "user_id": "u2", "ai_enabled": False},
        ])
        self.assertEqual(written, 2)
        self.assertFalse(self.rows()[0]["ai_enabled"])

    def test_source_label_is_preserved(self):
        self.recorder.record([{
            "text": "giá bao nhiêu", "msg_id": "source-1",
            "data_source": "live_extension",
        }])
        self.assertEqual(self.rows()[0]["data_source"], "live_extension")

    def test_empty_text_is_dropped(self):
        self.assertEqual(self.recorder.record([{"text": "   "}]), 0)


class CatalogGuardTests(unittest.TestCase):
    def setUp(self):
        # Không được ghi vào data/ thật: file danh mục LIVE là dữ liệu vận hành.
        self.temp = tempfile.TemporaryDirectory()
        self.patched = Path(self.temp.name) / "live_products_current.json"
        self.original = server_module.LIVE_CATALOG_PATH
        server_module.LIVE_CATALOG_PATH = self.patched

    def tearDown(self):
        server_module.LIVE_CATALOG_PATH = self.original
        self.temp.cleanup()

    def test_empty_snapshot_never_wipes_a_good_catalog(self):
        store = LiveCatalogStore()
        store.snapshot = {}
        good = {"room_id": "r1", "pin_id": "p1", "products": [
            {"product_id": "p1", "title": "TC01", "original_price": "120.000₫"}
        ]}
        saved = store.sync(good)
        self.assertEqual(saved["product_count"], 1)
        kept = store.sync({})
        self.assertEqual(kept["product_count"], 1)
        self.assertEqual(store.snapshot["product_count"], 1)

    def test_empty_snapshot_for_new_room_drops_old_prices_and_pin(self):
        store = LiveCatalogStore()
        store.snapshot = {}
        store.sync({"room_id": "r1", "pin_id": "p1", "products": [
            {"product_id": "p1", "original_price": "120.000₫"}
        ]})
        current = store.sync({"room_id": "r2", "products": []})
        self.assertEqual(current["room_id"], "r2")
        self.assertEqual(current["pin_id"], "")
        self.assertEqual(current["products"], [])


    def test_prelive_snapshot_saves_phase_and_owner(self):
        store = LiveCatalogStore()
        store.snapshot = {}
        saved = store.sync({"room_id": "", "phase": "prelive", "host": {"user_id": "777"},
                            "products": [{"product_id": "a1"}]})
        self.assertEqual(saved["phase"], "prelive")
        self.assertEqual(saved["owner_key"], "uid:777")
        self.assertEqual(saved["product_count"], 1)
        live = store.sync({"room_id": "r9", "host": {"user_id": "777"}, "products": [{"product_id": "b1"}]})
        self.assertEqual(live["phase"], "live")
        self.assertEqual(live["products"][0]["product_id"], "b1")

    def test_prelive_does_not_overwrite_active_live_same_owner(self):
        store = LiveCatalogStore()
        store.snapshot = {}
        store.sync({"room_id": "r1", "host": {"user_id": "777"}, "products": [{"product_id": "p1"}]})
        kept = store.sync({"room_id": "", "phase": "prelive", "host": {"user_id": "777"},
                           "products": [{"product_id": "x1"}]})
        self.assertEqual(kept["skipped"], "live_active")
        self.assertEqual(store.snapshot["room_id"], "r1")
        self.assertEqual(store.snapshot["products"][0]["product_id"], "p1")
        # Phiên LIVE cũ (>10 phút) thì giỏ prelive được thay.
        stale = dict(store._snapshot)
        stale["synced_at"] = "2000-01-01T00:00:00+07:00"
        store.snapshot = stale
        fresh = store.sync({"room_id": "", "phase": "prelive", "host": {"user_id": "777"},
                            "products": [{"product_id": "x1"}]})
        self.assertNotIn("skipped", fresh)
        self.assertEqual(fresh["products"][0]["product_id"], "x1")

    def test_other_owner_replaces_catalog_with_warning(self):
        store = LiveCatalogStore()
        store.snapshot = {}
        store.sync({"room_id": "r1", "host": {"user_id": "777"}, "products": [{"product_id": "p1"}]})
        with self.assertLogs("kid-te.core", level="WARNING"):
            other = store.sync({"room_id": "", "phase": "prelive", "host": {"user_id": "888"},
                                "products": [{"product_id": "z1"}]})
        self.assertEqual(other["owner_key"], "uid:888")
        self.assertEqual(other["products"][0]["product_id"], "z1")


class PricePolicyUnitTests(unittest.TestCase):
    def test_quotable_price_prefers_original_and_hides_sale(self):
        context = price_context({"original_price": "289.000₫", "sale_price": "226.380₫"})
        self.assertEqual(context["quotable_price"], "289.000₫")
        self.assertNotIn("226.380₫", json.dumps(context, ensure_ascii=False))

    def test_forbidden_phrases_are_detected_case_and_accent_insensitively(self):
        signal = {"kind": "shopper_visible_price_report"}
        self.assertTrue(violates_price_policy("185k KHONG CO nha anh", signal))
        self.assertTrue(violates_price_policy("Giá đúng là 295k", signal))
        self.assertFalse(violates_price_policy("Vậy tài khoản anh có mã rồi", signal))
        self.assertEqual(violates_price_policy("185k không có", {}), "")

    def test_denial_guard_only_fires_on_availability_questions(self):
        self.assertTrue(denies_catalog("có thuốc phun kiến không", "Không có nha anh"))
        self.assertFalse(denies_catalog("giá bao nhiêu", "Không có nha anh"))
        self.assertFalse(
            denies_catalog("có thuốc phun kiến không", "Anh đang gặp ở cây gì?")
        )


_STUB_TMP = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)  # removed at interpreter exit


class StubEngine:
    def __init__(self):
        self.raw_stream = RawStreamRecorder(Path(_STUB_TMP.name), enabled=True)
        self.missing = MissingReporter()
        self.observed = []
        self.video_file = self.raw_stream.dir / "sample.mp4"
        self.video_file.write_bytes(b"0123456789")

    def observe(self, payload):
        self.observed.append(payload)
        return {"ok": True, "written": self.raw_stream.record(payload.get("events"))}

    def health(self):
        return {"ok": True}

    def dashboard_state(self):
        return {"ok": True, "backend": {"state": "ready"}}

    def backup_now(self, payload=None):
        return {"ok": True, "created": True, "backup_dir": "verified"}

    backup_now_and_refresh = backup_now

    def heartbeat(self, payload):
        return {"ok": True, "live_running": payload.get("live_running") is True}

    def diagnostics_state(self):
        return {"ok": True, "checks": []}

    def diagnostics_fix(self):
        return {"ok": True, "created": []}

    def support_report_preview(self):
        return {"ok": True, "files": ["manifest.json"], "will_send": False}

    def support_report_create(self):
        return {"ok": True, "path": "local.zip", "sent": False}

    def settings_state(self):
        return {"ok": True, "models": ["model-a"], "selected_model": "model-a"}

    def workspace_catalog(self):
        return {"ok": True, "resources": [
            {"id": "profiles", "label": "Hồ sơ kỹ thuật", "editable": True},
        ]}

    def workspace_list(self, resource, query=None):
        return {"ok": True, "resource": resource, "items": [{
            "id": "SP-01", "title": "Đạm cá",
        }], "page": 1, "limit": 50, "total": 1, "pages": 1}

    def workspace_detail(self, resource, record_id):
        return {"ok": True, "resource": resource, "id": record_id,
                "data": {"id": record_id, "title": "Đạm cá"},
                "revision": "r1", "editable": True, "history": []}

    def workspace_patch(self, resource, record_id, payload):
        if payload.get("expected_revision") == "stale":
            raise ValueError("revision_conflict")
        return {"ok": True, "resource": resource, "id": record_id,
                "revision": "r2", "applied": True}

    def workspace_correct(self, payload):
        return {"ok": True, "correction_id": "c1", "applied": True}

    def workspace_undo_correction(self, correction_id):
        return {"ok": True, "correction_id": correction_id, "undone": True}

    def workspace_rollback(self, resource, record_id):
        return {"ok": True, "resource": resource, "id": record_id,
                "revision": "r0"}

    def workspace_delete(self, resource, record_id, payload=None):
        return {"ok": True, "trash_id": "t1", "undo_available": True}

    def workspace_restore(self, resource, record_id):
        return {"ok": True, "resource": resource, "id": record_id, "restored": True}

    def workspace_trash(self, query=None):
        return {"ok": True, "resource": "trash", "items": [], "page": 1,
                "limit": 50, "total": 0, "pages": 1}

    def workspace_purge_expired(self):
        return {"ok": True, "purged": 0}

    def workspace_save_video_draft(self, video_id, payload):
        return {"ok": True, "video_id": video_id, "saved": True}

    def workspace_dose_test(self, profile_id, payload):
        return {"ok": True, "liters": payload.get("liters"), "results": [{
            "amount": "16 lít dùng 20 ml", "source": "20 ml cho bình 16 lít",
        }]}

    def workspace_resolve_video_duplicate(self, video_id, payload):
        return {"ok": True, "decision": payload.get("action"),
                "video_id": video_id}

    def video_media_path(self, video_id):
        return self.video_file

    def save_settings(self, payload):
        return {"ok": True, "model": payload.get("model")}

    def test_audio_device(self, payload):
        return {"ok": True, "heard": True,
                "message": "Đã nghe thấy âm thanh video từ thiết bị này."}

    def video_preflight(self):
        return {"ok": True, "catalog_ready": True, "product_count": 41}

    def video_analysis_jobs(self):
        return {"ok": True, "jobs": []}

    def video_analysis_status(self, job_id, include_draft=False):
        return {"ok": True, "job_id": job_id, "draft": {} if include_draft else None}

    def video_upload_start(self, payload):
        return {"ok": True, "job_id": "a" * 32, **payload}

    def video_upload_finish(self, payload):
        return {"ok": True, "state": "queued", **payload}

    def video_analysis_resume(self, payload):
        return {"ok": True, "state": "queued", **payload}

    def video_analysis_publish(self, payload):
        return {"ok": True, "video_id": payload.get("video_id")}

    class _VideoAnalysis:
        def append_chunk(self, job_id, offset, stream, length):
            return {"ok": True, "job_id": job_id, "offset": offset,
                    "body": stream.read(length).decode("ascii")}

    video_analysis = _VideoAnalysis()


class HttpLayerTests(unittest.TestCase):
    def setUp(self):
        self.engine = StubEngine()
        Handler.engine = self.engine
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, args=(0.1,), daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def post(self, path, body, origin="chrome-extension://abc"):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "Origin": origin},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def get(self, path, origin=""):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", headers={"Origin": origin},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.headers.get_content_type(), response.read().decode("utf-8")

    def post_bytes(self, path, body, origin=""):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=body,
            headers={"Content-Type": "application/octet-stream", "Origin": origin},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def patch(self, path, body, origin=""):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode("utf-8"), method="PATCH",
            headers={"Content-Type": "application/json", "Origin": origin},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def delete(self, path, body, origin=""):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode("utf-8"), method="DELETE",
            headers={"Content-Type": "application/json", "Origin": origin},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_shutdown_accepts_only_local_ui(self):
        shutdown = mock.Mock()
        self.engine.request_shutdown = shutdown
        self.assertTrue(self.post("/api/shutdown", {}, origin="")["ok"])
        shutdown.assert_called_once()
        for origin in ("https://evil.example", "chrome-extension://abc"):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.post("/api/shutdown", {}, origin=origin)
            self.assertEqual(caught.exception.code, 403)
        shutdown.assert_called_once()

    def test_backup_v2_start_uses_local_job_endpoint_and_rejects_extension_origin(self):
        self.engine.system_post = mock.Mock(return_value={'ok':True,'accepted':True,'job_id':'fixture-job'})
        body = {'password':'fixture password'}
        request = urllib.request.Request(f'http://127.0.0.1:{self.port}/api/system/backup/v2/start',
            data=json.dumps(body).encode(), headers={'Content-Type':'application/json'})
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 202)
            result = json.loads(response.read())
            self.assertEqual(result['job_id'], 'fixture-job')
            self.assertNotIn('password', result)
        self.engine.system_post.assert_called_once_with('backup/v2/start', body)
        for origin in ('chrome-extension://fixture', 'https://evil.example'):
            with self.assertRaises(urllib.error.HTTPError) as rejected:
                self.post('/api/system/backup/v2/start', body, origin=origin)
            self.assertEqual(rejected.exception.code, 403)
        self.engine.system_post.assert_called_once()

    def test_delivery_routes_dispatch_and_require_extension_origin(self):
        self.engine.delivery_operation = mock.Mock(return_value={'ok': True})
        payload = {'turn_id': 't', 'message_id': 'm', 'room_id': 'r'}
        self.assertTrue(self.post('/api/reply/claim', payload)['ok'])
        self.engine.delivery_operation.assert_called_once_with('tiktok', 'claim', payload)
        with self.assertRaises(urllib.error.HTTPError) as rejected:
            self.post('/api/shopee/reply/receipt', payload, origin='https://example.com')
        self.assertEqual(rejected.exception.code, 403)
        with self.assertRaises(urllib.error.HTTPError) as malformed:
            self.post('/api/reply/claim', [])
        self.assertEqual(malformed.exception.code, 400)
        self.engine.queue_operation = mock.Mock(return_value={'ok': True, 'state': 'queued'})
        self.assertEqual(self.post('/api/shopee/reply/queue/enqueue', payload)['state'], 'queued')
        self.engine.queue_operation.assert_called_once_with('shopee', 'enqueue', payload)
        self.engine.queue_operation.reset_mock()
        for route, platform in (('/api/tiktok/queue/cancel', 'tiktok'), ('/api/shopee/queue/cancel', 'shopee'),
                                ('/api/reply/queue/cancel', 'tiktok')):
            self.post(route, {'room_id': 'r', 'comment_id': 'c'})
            self.engine.queue_operation.assert_called_with(platform, 'cancel', {'room_id': 'r', 'comment_id': 'c'})

    def test_json_routes_reject_non_finite_nested_numbers(self):
        for number in ('NaN', 'Infinity', '-Infinity', '1e999'):
            body = ('{"nested":{"value":' + number + '}}').encode()
            with self.assertRaises(urllib.error.HTTPError) as malformed:
                self.post_bytes('/api/reply/claim', body, origin='chrome-extension://abc')
            self.assertEqual(malformed.exception.code, 400)

    def test_observe_endpoint_accepts_a_batch(self):
        result = self.post("/api/observe", {"events": [
            {"text": "giá bao nhiêu", "user_id": "u1", "unique_id": "a"},
            {"text": "@a 289k nha anh", "is_host": True},
        ]})
        self.assertTrue(result["ok"])
        self.assertEqual(result["written"], 2)

    def test_observe_endpoint_rejects_non_extension_origin(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.post("/api/observe", {"events": []}, origin="https://evil.example")
        self.assertEqual(caught.exception.code, 403)

    def test_shopee_auto_sync_is_extension_only(self):
        self.engine.sync_shopee_catalog = lambda payload: {"ok": True, "session_id": payload["session_id"]}
        assert self.post("/api/shopee/sync", {"session_id": "123", "products": []})["session_id"] == "123"
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.post("/api/shopee/sync", {"session_id": "123"}, origin="https://evil.example")
        assert caught.exception.code == 403

    def test_shopee_mapping_endpoint_reads_and_saves_for_extension(self):
        self.engine.shopee_mapping_state = lambda: {"ok": True, "videos": {}}
        self.engine.save_shopee_mappings = lambda payload: {
            "ok": True, "video_id": payload["video_id"]}
        _, raw = self.get("/api/shopee/mappings", origin="chrome-extension://abc")
        self.assertEqual(json.loads(raw)["videos"], {})
        _, raw = self.get("/api/shopee/mappings", origin="")
        self.assertEqual(json.loads(raw)["videos"], {})
        result = self.post("/api/shopee/mappings", {"video_id": "live1"})
        self.assertEqual(result["video_id"], "live1")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("/api/shopee/mappings", origin="https://evil.example")
        self.assertEqual(caught.exception.code, 403)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.post("/api/shopee/mappings", {"video_id": "live1"}, origin="https://evil.example")
        self.assertEqual(caught.exception.code, 403)

    def test_shopee_ai_routes_require_extension_and_keep_delivery_separate(self):
        class ShopeeAi:
            def reply(self, payload):
                return {"ok": True, "should_send": False, "reason": payload["comment"]}

            def delivery(self, payload):
                return {"ok": True, "sent": bool(payload.get("message_id"))}

        self.engine.shopee_ai = lambda: ShopeeAi()
        self.engine.responder = type("Responder", (), {
            "check_balance": lambda _self: {"ok": True, "deepseek_available": True}
        })()
        _, raw = self.get("/api/shopee/ai/status", origin="chrome-extension://abc")
        self.assertTrue(json.loads(raw)["deepseek_available"])
        _, raw = self.get("/api/shopee/ai/status", origin="")
        self.assertTrue(json.loads(raw)["deepseek_available"])
        self.assertEqual(self.post("/api/shopee/reply", {"comment": "xin chào"})["reason"], "xin chào")
        self.assertTrue(self.post("/api/shopee/delivery", {"message_id": "m1"})["sent"])
        for path in ("/api/shopee/ai/status", "/api/shopee/reply"):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                if path.endswith("status"):
                    self.get(path, origin="https://evil.example")
                else:
                    self.post(path, {"comment": "hello"}, origin="https://evil.example")
            self.assertEqual(caught.exception.code, 403)

    def test_app_and_dashboard_are_available_only_to_local_ui(self):
        content_type, page = self.get("/app")
        self.assertEqual(content_type, "text/html")
        self.assertIn("Kid TE", page)
        self.assertIn("Trợ lý vận hành LIVE", page)
        self.assertIn("bootstrap.js", page)
        self.assertNotIn("Sự kiện timeline (JSON)", page)
        self.assertNotIn("Duyệt tất cả</button>", page)
        root_content_type, root_page = self.get("/")
        self.assertEqual(root_content_type, "text/html")
        self.assertEqual(root_page, page)
        _, raw = self.get("/api/dashboard")
        self.assertEqual(json.loads(raw)["backend"]["state"], "ready")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("/api/dashboard", origin="https://evil.example")
        self.assertEqual(caught.exception.code, 403)

    def test_operator_module_assets_are_explicitly_served(self):
        modules = [
            "/assets/app/bootstrap.js", "/assets/app/core/api.js",
            "/assets/app/core/store.js", "/assets/app/core/router.js",
            "/assets/app/shell.js", "/assets/app/ui/kit.js",
            "/assets/app/pages/overview.js", "/assets/app/pages/inbox.js",
            "/assets/app/pages/catalog.js", "/assets/app/pages/video/studio.js",
            "/assets/app/pages/automation.js", "/assets/app/pages/system.js",
        ]
        for path in modules:
            content_type, body = self.get(path)
            self.assertEqual(content_type, "application/javascript")
            self.assertTrue(body.strip())
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("/assets/app/pages/not-allowed.js")
        self.assertEqual(caught.exception.code, 404)

    def test_workspace_drilldown_edit_correction_and_rollback_are_local_only(self):
        _, raw = self.get("/api/workspace/profiles?page=1&limit=50&q=dam")
        self.assertEqual(json.loads(raw)["total"], 1)
        _, raw = self.get("/api/workspace/profiles/SP-01")
        self.assertEqual(json.loads(raw)["revision"], "r1")
        saved = self.patch("/api/workspace/profiles/SP-01", {
            "expected_revision": "r1", "changes": {"ten_san_pham": "Mới"},
            "reason": "Đối chiếu nhãn",
        })
        self.assertTrue(saved["applied"])
        corrected = self.post("/api/workspace/corrections", {
            "resource": "facts", "record_id": "u1:dose",
            "changes": {"fact_value": "20ml"}, "reason": "Khách xác nhận",
        }, origin="")
        self.assertTrue(corrected["applied"])
        rolled = self.post("/api/workspace/profiles/SP-01/rollback", {}, origin="")
        self.assertEqual(rolled["revision"], "r0")
        self.assertTrue(self.delete("/api/workspace/profiles/SP-01", {
            "expected_revision": "r1",
        })["undo_available"])
        self.assertTrue(self.post(
            "/api/workspace/profiles/SP-01/restore", {}, origin="",
        )["restored"])
        self.assertTrue(self.post("/api/workspace/videos/video-1/draft", {
            "expected_revision": "r1",
            "draft": {"events": [], "script_contexts": []},
        }, origin="")["saved"])
        self.assertEqual(self.post(
            "/api/workspace/profiles/SP-01/dose-test", {"liters": 16}, origin="",
        )["results"][0]["amount"], "16 lít dùng 20 ml")
        self.assertEqual(self.post(
            "/api/workspace/videos/video-1/duplicates",
            {"other_id": "video-2", "action": "keep_both"}, origin="",
        )["decision"], "keep_both")
        self.assertTrue(self.post(
            "/api/workspace/corrections/c1/undo", {}, origin="",
        )["undone"])
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.patch("/api/workspace/profiles/SP-01", {
                "expected_revision": "r1", "changes": {}, "reason": "x",
            }, origin="https://evil.example")
        self.assertEqual(caught.exception.code, 403)

    def test_workspace_revision_conflict_returns_409(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.patch("/api/workspace/profiles/SP-01", {
                "expected_revision": "stale", "changes": {"ten_san_pham": "Mới"},
                "reason": "x",
            })
        self.assertEqual(caught.exception.code, 409)


    def test_video_media_supports_browser_byte_ranges(self):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/video/media/video-1",
            headers={"Range": "bytes=2-4"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.headers["Content-Range"], "bytes 2-4/10")
            self.assertEqual(response.read(), b"234")
        _, raw = self.get("/api/video/analysis/status?job_id=abc&include_draft=1")
        self.assertEqual(json.loads(raw)["draft"], {})
        uploaded = self.post_bytes(
            "/api/video/upload/chunk?job_id=" + "a" * 32 + "&offset=7", b"chunk",
        )
        self.assertEqual(uploaded["body"], "chunk")
        self.assertEqual(uploaded["offset"], 7)
        resumed = self.post("/api/video/analysis/resume", {"job_id": "a" * 32}, origin="")
        self.assertEqual(resumed["state"], "queued")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.post_bytes(
                "/api/video/upload/chunk?job_id=" + "a" * 32 + "&offset=0",
                b"chunk", origin="https://evil.example",
            )
        self.assertEqual(caught.exception.code, 403)

    def test_backup_now_is_local_only(self):
        result = self.post("/api/backup", {}, origin="")
        self.assertTrue(result["created"])
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.post("/api/backup", {}, origin="https://evil.example")
        self.assertEqual(caught.exception.code, 403)

    def test_extension_heartbeat_requires_extension_origin(self):
        result = self.post("/api/heartbeat", {"live_running": True})
        self.assertTrue(result["live_running"])
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.post("/api/heartbeat", {}, origin="")
        self.assertEqual(caught.exception.code, 403)

    def test_diagnostics_and_support_report_are_local_only(self):
        _, raw = self.get("/api/support/preview")
        self.assertFalse(json.loads(raw)["will_send"])
        self.assertTrue(self.post("/api/diagnostics/fix", {}, origin="")["ok"])
        self.assertFalse(self.post("/api/support/create", {}, origin="")["sent"])
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("/api/support/preview", origin="https://evil.example")
        self.assertEqual(caught.exception.code, 403)

    def test_settings_only_accepts_local_dropdown_value_request(self):
        _, raw = self.get("/api/settings")
        self.assertEqual(json.loads(raw)["models"], ["model-a"])
        self.assertEqual(self.post("/api/settings", {"model": "model-a"}, origin="")["model"], "model-a")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.post("/api/settings", {"model": "model-a"}, origin="https://evil.example")
        self.assertEqual(caught.exception.code, 403)

    def test_audio_listen_check_is_local_only(self):
        result = self.post("/api/audio/test", {"audio_device_id": "loopback"}, origin="")
        self.assertTrue(result["heard"])
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.post("/api/audio/test", {"audio_device_id": "loopback"},
                      origin="https://evil.example")
        self.assertEqual(caught.exception.code, 403)


def test_audio_device_restart_failure_restores_previous_setting():
    class Runtime:
        def __init__(self):
            self.restarts = 0

        def snapshot(self):
            return {"plugins": [{"id": "dong-bo-video", "state": "dang_chay"}]}

        def restart(self, plugin_id):
            self.restarts += 1
            return {"ok": self.restarts > 1, "plugin_id": plugin_id}

    engine = ReplyEngine.__new__(ReplyEngine)
    engine.responder = types.SimpleNamespace()
    engine.audio_device_id = "old-loopback"
    engine.plugin_runtime = Runtime()
    engine.settings_state = lambda: {"audio_devices": [{
        "id": "new-loopback", "loopback": True, "disabled": False,
    }]}
    saved = []
    with mock.patch("config_settings.save_audio_device",
                    side_effect=lambda value: saved.append(value) or {"ok": True}), \
            mock.patch.dict(os.environ, {"LIVE_RE_AUDIO_DEVICE": "old-loopback"},
                            clear=False):
        with unittest.TestCase().assertRaisesRegex(ValueError, "audio_device_restart_failed"):
            engine.save_settings({"audio_device_id": "new-loopback"})
        assert os.environ["LIVE_RE_AUDIO_DEVICE"] == "old-loopback"
    assert saved == ["new-loopback", "old-loopback"]
    assert engine.audio_device_id == "old-loopback"
    assert engine.plugin_runtime.restarts == 2


def test_shopee_catalog_starts_shared_video_once_after_valid_sync():
    engine = ReplyEngine.__new__(ReplyEngine)
    engine._lock = threading.Lock()
    engine.video_timelines = lambda: {"timelines": {}}
    engine.shopee_mapping_store = mock.Mock()
    engine.shopee_mapping_store.sync.return_value = {"ok": False}
    engine.plugin_runtime = mock.Mock()
    engine.plugin_runtime.start.return_value = {"ok": True}
    assert not engine.sync_shopee_catalog({})["ok"]
    engine.plugin_runtime.start.assert_not_called()
    engine.shopee_mapping_store.sync.side_effect = lambda *args: {"ok": True}
    assert engine.sync_shopee_catalog({})["video_sync"]["ok"]
    engine.sync_shopee_catalog({})
    engine.plugin_runtime.start.assert_called_once_with("dong-bo-video")


def test_audio_listen_check_reports_detected_signal():
    class Samples:
        def __mul__(self, other):
            return self

        def mean(self):
            return 0.0004

    class Capture:
        error = None

        def __init__(self, selector, seconds):
            self.selector = selector

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def latest(self):
            return Samples()

    engine = ReplyEngine.__new__(ReplyEngine)
    engine.settings_state = lambda: {"audio_devices": [{
        "id": "loopback", "loopback": True, "disabled": False,
    }]}
    with mock.patch("audio_capture.RollingCapture", Capture), \
            mock.patch("server.time.sleep"):
        result = engine.test_audio_device({"audio_device_id": "loopback"})
    assert result["heard"] is True
    assert "nghe thấy" in result["message"]


# Tiêu đề thật từ phòng LIVE 7683842624828246804, cắt ngắn. Chúng nhồi từ khoá
# SEO nên là bài kiểm tra đúng cho việc đoán khách đang nói tới SKU nào.
REAL_TITLES = [
    ("p-tc01", "Kích Rễ Cực Mạnh TC01 - Chuyên Dùng Cho Mai Vàng, Cây cảnh, Bonsai, Rau Màu (chai 100ml) humic tưới"),
    ("p-dam1l", "Đạm Cá Amino 1L Chất Hữu Cơ 20% Amino Acid 10% Vitamin C B6 Giúp Cây Bật Mầm Xanh Lá Nuôi Trái"),
    ("p-botri", "BO TRI ONE | Chế Phẩm Top Bo One Tri 200ml - Công Nghệ Lưu Dẫn 2 Chiều Mới - Dạng Sữa SC phan bon"),
    ("p-dam5l", "Đạm Cá Hữu Cơ Amino Giúp Cây Bật Mầm, Xanh Lá, Bung Cành, Đẻ Nhánh Mạnh, Nuôi Trái (Can 5 Lít) cô đặc"),
    ("p-humic5l", "HUMIC Can 5 Lít Nông Dược Toàn Cầu – Phát triển rễ, Vọt đọt, Xanh lá, Mập cây, Cải tạo đất"),
    ("p-vuabotri", "VUA BO TRI | Chế Phẩm NO FA RA 350SC Dạng Sữa SC Có Tác Dụng Mát Cây Giúp Cây Trồng Khỏe Mạnh"),
    ("p-canxibo", "SIÊU CANXI BO DẠNG SỮA - PHÂN BÓN LÁ TĂNG RA HOA ĐẬU QUẢ, CHỐNG RỤNG TRÁI"),
    ("p-chuyenot", "Phân Bón Chuyên Ớt (50gr) - Bóng quả, chín đều - Nông Dược Toàn Cầu trichoderma dế kali npk kíchrễ đạm tinh"),
    ("p-tricho", "Phân bón TRICHODERMA Toàn Cầu – Ủ phân, cải tạo đất tơi xốp, giúp cây trồng hấp thu dinh dưỡng tốt"),
    ("p-combo", "Combo Đạm Cá + Humic Nông Dược Toàn Cầu Giúp Cây Bật Mầm, Xanh Lá, Bung Cành, Đẻ Nhánh"),
]


class SelectLiveProductTests(unittest.TestCase):
    def setUp(self):
        self.products = [
            {"product_id": pid, "title": title} for pid, title in REAL_TITLES
        ]
        self.pin = "p-botri"

    def pick(self, comment):
        return select_live_product(comment, self.products, self.pin)["product_id"]

    def test_named_products_are_resolved_despite_seo_stuffed_titles(self):
        self.assertEqual(self.pick("tc01 dùng sao"), "p-tc01")
        self.assertEqual(self.pick("bọ trĩ giá bao nhiêu"), "p-botri")
        self.assertEqual(self.pick("trichoderma bao nhiêu"), "p-tricho")
        self.assertEqual(self.pick("humic 5 lít bao nhiêu"), "p-humic5l")

    def test_volume_decides_between_one_litre_and_five_litre(self):
        self.assertEqual(self.pick("đạm cá 1l giá"), "p-dam1l")
        self.assertEqual(self.pick("đạm cá chai 1 lít bao nhiêu"), "p-dam1l")
        self.assertEqual(self.pick("đạm cá 5 lít bao nhiêu"), "p-dam5l")
        self.assertEqual(self.pick("đạm cá can 5l giá sao"), "p-dam5l")

    def test_question_words_never_steal_the_product(self):
        # "bao nhiêu", "chai này", "thuốc" từng đủ để nhảy sang một SKU khác.
        for vague in ("chai này bao nhiêu", "cái này giá sao em",
                      "thuốc này giá bao nhiêu", "alo", "chào em",
                      "thuốc phun kiến có không"):
            self.assertEqual(self.pick(vague), self.pin, vague)

    def test_combo_does_not_outrank_the_single_sku(self):
        self.assertEqual(self.pick("đạm cá 5 lít bao nhiêu"), "p-dam5l")
        self.assertEqual(self.pick("combo đạm cá humic bao nhiêu"), "p-combo")

    def test_seo_keywords_in_the_title_tail_do_not_steal_another_sku(self):
        # 12/09 LIVE: "Phi 200l thì pha bao nhiêu kích rễ với đạm cá shop"
        # bị gán cho HUMIC, vì đuôi SEO của HUMIC có nhét "đạm cá cô đặc" và
        # "kíchrễ". Tên thật của Humic nằm ở ĐẦU tiêu đề, hai từ kia ở ĐUÔI.
        products = [
            {"product_id": "p-humic",
             "title": "HUMIC Can 5 Lít Nông Dược Toàn Cầu – Phát triển rễ, Vọt đọt, "
                      "Xanh lá, Mập cây, Cải tạo đất, Tiết kiệm chi phí phân bón, "
                      "Tăng hấp thụ phân bón trong rau đạm cá cô đặc cay hữu phân "
                      "bón mỹ phân bón tinh npk kichre"},
            {"product_id": "p-damca",
             "title": "Đạm Cá Hữu Cơ Amino Giúp Cây Bật Mầm, Xanh Lá, Mập Cây (5 lít)"},
            {"product_id": "p-kichre",
             "title": "Kích Rễ Cực Mạnh TC01 - Chuyên Dùng Cho Mai Vàng, Cây cảnh"},
        ]
        chon = select_live_product("đạm cá pha bao nhiêu", products, "p-kichre")
        self.assertEqual(chon["product_id"], "p-damca",
                         "đuôi SEO của Humic không được cướp câu hỏi đạm cá")

    def test_dose_question_words_never_steal_the_product(self):
        # "a6 pha bình 16 lít bao nhiêu gam" từng chọn ra Magie Kẽm, chỉ vì
        # tiêu đề SEO của nó có sẵn "pha", "bình", "gam".
        for vague in ("pha bình 16 lít bao nhiêu gam", "pha bình 20 lít sao",
                      "liều phun tưới thế nào", "bao nhiêu ml cho bình 25 lít"):
            self.assertEqual(self.pick(vague), self.pin, vague)


class ModelRetryLadderTests(unittest.TestCase):
    """Model đốt hết hạn mức suy nghĩ rồi im thì phải hạ nấc, không được bỏ cuộc.

    12/09 mất 3 câu hỏi thật vì bản cũ chỉ hạ high->low, mà câu hỏi liều vốn
    đã chạy ở mức low sẵn nên không được thử lại lần nào.
    """

    class _Choice:
        def __init__(self, content, finish_reason):
            self.message = types.SimpleNamespace(content=content)
            self.finish_reason = finish_reason

    class _Response:
        def __init__(self, content, finish_reason):
            self.choices = [ModelRetryLadderTests._Choice(content, finish_reason)]
            self.usage = types.SimpleNamespace(
                prompt_tokens=100, completion_tokens=50,
                completion_tokens_details=types.SimpleNamespace(reasoning_tokens=50))

    def _responder(self, ket_qua, tat_suy_nghi=False, muc="high"):
        responder = server_module.DeepSeekResponder.__new__(
            server_module.DeepSeekResponder)
        # Dat thang tren instance: thang thu lai khong duoc phu thuoc vao
        # .env cua may dang chay test.
        responder.disable_thinking = tat_suy_nghi
        responder.reasoning_effort = muc
        self.goi = []
        self.tran_token = []
        self.model_requests = []

        def create(**kwargs):
            self.model_requests.append(kwargs)
            self.goi.append(kwargs.get("reasoning_effort", "TAT-SUY-NGHI"))
            self.tran_token.append(kwargs.get("max_tokens"))
            return ModelRetryLadderTests._Response(*ket_qua[len(self.goi) - 1])

        responder.client = types.SimpleNamespace(
            chat=types.SimpleNamespace(
                completions=types.SimpleNamespace(create=create)))
        return responder

    def test_v2_model_messages_rewrite_once_and_never_silent_cut(self):
        bad = json.dumps({'action':'reply','messages':['😀'*20,'Không được cắt']}, ensure_ascii=False)
        good = json.dumps({'action':'reply','messages':['Tin một đủ ý.','Tin hai đủ ý.']}, ensure_ascii=False)
        responder = self._responder([(bad, 'stop'), (good, 'stop')], tat_suy_nghi=True)
        result = responder.reply('Mai', 'hướng dẫn', {'delivery_protocol_version':2}, 30)
        self.assertEqual(result['messages'], ['Tin một đủ ý.','Tin hai đủ ý.'])
        self.assertEqual(len(self.goi), 2)
        prompt = self.model_requests[0]['messages'][-1]['content']
        self.assertIn('Giới hạn áp dụng từng tin', prompt)
        self.assertNotIn('Độ dài reply:', prompt)
        responder = self._responder([(bad, 'stop'), (bad, 'stop')], tat_suy_nghi=True)
        result = responder.reply('Mai', 'hướng dẫn', {'delivery_protocol_version':2}, 30)
        # Viết lại vẫn hỏng: gộp thành MỘT tin cắt gọn theo giới hạn thay vì im lặng.
        self.assertEqual(result['action'], 'reply')
        self.assertEqual(len(result['messages']), 1)
        self.assertLessEqual(len(result['messages'][0].encode('utf-16-le')) // 2, 30)
        self.assertTrue(result['metrics'].get('multi_message_trimmed'))
        self.assertEqual(len(self.goi), 2)

    def test_transient_api_errors_retry_twice_but_never_auth(self):
        calls = []

        class APITimeoutError(Exception):
            pass

        class AuthenticationError(Exception):
            status_code = 401

        def make(errors):
            ok = ModelRetryLadderTests._Response(json.dumps({'action': 'reply', 'reply': 'Dạ ok nha'}), 'stop')
            seq = list(errors) + [ok]
            responder = self._responder([('{}', 'stop')], tat_suy_nghi=True)

            def create(**kwargs):
                calls.append(1)
                item = seq[len(calls) - 1] if len(calls) <= len(seq) else seq[-1]
                if isinstance(item, Exception):
                    raise item
                return item
            responder.client = types.SimpleNamespace(chat=types.SimpleNamespace(
                completions=types.SimpleNamespace(create=create)))
            return responder

        with mock.patch('time.sleep'):
            result = make([APITimeoutError(), APITimeoutError()]).reply('Mai', 'chào', {}, 100)
            self.assertEqual(result['action'], 'reply')
            self.assertEqual(len(calls), 3)
            calls.clear()
            result = make([AuthenticationError()]).reply('Mai', 'chào', {}, 100)
            self.assertEqual(result['action'], 'skip')
            self.assertEqual(len(calls), 1)

    def test_thinking_da_tat_van_duoc_thu_lai_voi_tran_token_rong_hon(self):
        """Mac dinh cua config la TAT thinking, nen truoc day ladder = []
        va thang thu lai chua bao gio chay tren may that."""
        responder = self._responder([
            ("", "length"),
            ('{"action":"reply","reply":"Gốc 555k đó anh."}', "stop"),
        ], tat_suy_nghi=True)
        ket = responder.reply("Khách", "đạm cá giá nhiêu", {}, 100)
        self.assertEqual(ket["action"], "reply")
        self.assertEqual(len(self.goi), 2, "phai co dung mot lan thu lai")
        self.assertGreater(self.tran_token[1], self.tran_token[0],
                           "nac thu lai khi da tat thinking phai noi tran token")
        self.assertEqual(ket["metrics"]["nac_thu_lai"], "roi-rong")

    def test_thinking_da_tat_va_van_khong_ra_cau_thi_bao_skip(self):
        responder = self._responder([("", "length"), ("", "length")],
                                    tat_suy_nghi=True)
        ket = responder.reply("Khách", "hỏi gì đó", {}, 100)
        self.assertEqual(ket["action"], "skip")
        self.assertEqual(len(self.goi), 2, "khong duoc thu lai vo han")

    def test_a_dose_question_already_at_low_effort_still_gets_retried(self):
        responder = self._responder([
            ("", "length"),                                  # low  -> hết chỗ
            ('{"action":"reply","reply":"50g cho bình 20 lít nha."}', "stop"),
        ])
        ket = responder.reply("Khách", "bình 20 lít pha nhiêu gam",
                              {"verified_dose_calculations": [{"calculation": "x"}]},
                              100)
        self.assertEqual(ket["action"], "reply")
        self.assertEqual(self.goi, ["low", "TAT-SUY-NGHI"],
                         "đang ở low thì nấc kế tiếp phải là TẮT suy nghĩ")
        self.assertEqual(ket["metrics"]["nac_thu_lai"], "off")

    def test_a_high_effort_question_walks_the_whole_ladder(self):
        responder = self._responder([
            ("", "length"), ("", "length"),
            ('{"action":"reply","reply":"Gốc 555k đó anh."}', "stop"),
        ])
        ket = responder.reply("Khách", "đạm cá giá nhiêu", {}, 100)
        self.assertEqual(ket["action"], "reply")
        self.assertEqual(self.goi, ["high", "low", "TAT-SUY-NGHI"])

    def test_a_normal_answer_never_costs_a_second_call(self):
        responder = self._responder([
            ('{"action":"reply","reply":"Dạ 120k nha anh."}', "stop"),
        ])
        ket = responder.reply("Khách", "giá nhiêu", {}, 100)
        self.assertEqual(ket["action"], "reply")
        self.assertEqual(len(self.goi), 1, "trả lời được ngay thì không gọi lại")

    def test_reply_that_stays_too_long_is_not_silently_cut(self):
        long = json.dumps({'action': 'reply', 'reply': 'Nội dung đầy đủ ' * 30})
        responder = self._responder([(long, 'stop'), (long, 'stop')])
        result = responder.reply('Khách', 'Hỏi kỹ', {}, 100)
        self.assertEqual(result['action'], 'skip')
        self.assertEqual(result['missing_fields'], ['model_reply_too_long'])

    def test_second_shorten_pass_rescues_a_slightly_long_reply(self):
        def row(text):
            return (json.dumps({'action': 'reply', 'reply': text}), 'stop')
        responder = self._responder([row('a' * 130), row('b' * 110), row('Gọn rồi nha bà con.')])
        result = responder.reply('Khách', 'Hỏi kỹ', {}, 100)
        self.assertEqual(result['action'], 'reply')
        self.assertEqual(result['reply'], 'Gọn rồi nha bà con.')


class ModelCostMetricsTests(unittest.TestCase):
    """`[T12]` metrics phải mang đủ model + cache hit/miss để tính được chi
    phí phiên đúng giá — thiếu một trong hai thì metrics_store phải coi
    toàn bộ prompt là cache miss (đắt hơn thật còn hơn báo rẻ hơn thật),
    không phải vì server.py/plugin quên chuyển tiếp."""

    def _responder(self, usage_kwargs):
        responder = server_module.DeepSeekResponder.__new__(
            server_module.DeepSeekResponder)
        responder.model = "deepseek-v4-pro"

        class _Choice:
            message = types.SimpleNamespace(
                content='{"action":"reply","reply":"Dạ 120k nha anh."}')
            finish_reason = "stop"

        class _Response:
            choices = [_Choice()]
            usage = types.SimpleNamespace(**usage_kwargs)

        responder.client = types.SimpleNamespace(
            chat=types.SimpleNamespace(
                completions=types.SimpleNamespace(create=lambda **_k: _Response())))
        return responder

    def test_metrics_carry_model_name_and_cache_hit_miss_split(self):
        responder = self._responder({
            "prompt_tokens": 120, "completion_tokens": 40,
            "prompt_cache_hit_tokens": 100, "prompt_cache_miss_tokens": 20,
            "completion_tokens_details": types.SimpleNamespace(reasoning_tokens=0),
        })
        ket = responder.reply("Khách", "giá nhiêu", {}, 100)
        self.assertEqual(ket["metrics"]["model"], "deepseek-v4-pro")
        self.assertEqual(ket["metrics"]["prompt_cache_hit_tokens"], 100)
        self.assertEqual(ket["metrics"]["prompt_cache_miss_tokens"], 20)

    def test_metrics_default_cache_split_to_zero_when_provider_omits_it(self):
        """Provider/model cũ không trả cache hit/miss — metrics vẫn phải có
        đủ 2 khoá (giá trị 0), để `_reply_cost_usd` nhận biết là "thiếu" và
        tự coi toàn bộ prompt_tokens là cache miss, không phải KeyError."""
        responder = self._responder({
            "prompt_tokens": 120, "completion_tokens": 40,
            "completion_tokens_details": types.SimpleNamespace(reasoning_tokens=0),
        })
        ket = responder.reply("Khách", "giá nhiêu", {}, 100)
        self.assertEqual(ket["metrics"]["prompt_cache_hit_tokens"], 0)
        self.assertEqual(ket["metrics"]["prompt_cache_miss_tokens"], 0)

    def test_tracked_result_forwards_model_and_cache_fields_to_metrics_store(self):
        """`_tracked_result()` từng có danh sách khoá cố định — nếu quên
        thêm `model`/`prompt_cache_hit_tokens`/`prompt_cache_miss_tokens`
        vào đó thì mọi tính toán chi phí ở metrics_store sẽ luôn thiếu dữ
        liệu dù plugin đã tính đúng."""
        engine = server_module.ReplyEngine.__new__(server_module.ReplyEngine)
        recorded = {}
        engine.metrics = types.SimpleNamespace(
            record=lambda kind, **kwargs: recorded.update(kwargs))
        engine._tracked_result({"should_send": True}, metrics={
            "model": "deepseek-v4-pro", "model_ms": 10,
            "prompt_tokens": 120, "prompt_cache_hit_tokens": 100,
            "prompt_cache_miss_tokens": 20, "completion_tokens": 40,
        })
        self.assertEqual(recorded["model"], "deepseek-v4-pro")
        self.assertEqual(recorded["prompt_cache_hit_tokens"], 100)
        self.assertEqual(recorded["prompt_cache_miss_tokens"], 20)


class MappingToolTests(unittest.TestCase):
    """Công cụ gán hồ sơ: chủ dự án tự duyệt SKU mới, không sửa JSON tay."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.catalog_path = root / "knowledge.json"
        self.catalog_path.write_text(json.dumps({"nhom_san_pham": [{"san_pham": [
            {"san_pham_id": "SP-03", "ten_san_pham": "Kích Rễ Giâm Cành TC01",
             "approved_for_live": True, "shop_product_ids": ["p1"],
             "huong_dan_su_dung": "Pha 10ml cho bình 16 lít"},
            {"san_pham_id": "SP-09", "ten_san_pham": "Sạch Sâu Đất",
             "huong_dan_su_dung": "Rải quanh gốc"},
        ]}]}, ensure_ascii=False), encoding="utf-8")
        profiles_path = root / "profiles.json"
        profiles_path.write_text('{"products":{}}', encoding="utf-8")
        self.knowledge = KnowledgeBase(self.catalog_path, profiles_path)
        catalog = LiveCatalogStore()
        catalog.snapshot = {"room_id": "r1", "pin_id": "p1", "products": [
            {"product_id": "p1", "bag_index": 1,
             "title": "Kích Rễ Cực Mạnh TC01 chai 100ml", "original_price": "120.000₫"},
            {"product_id": "p5", "bag_index": 5,
             "title": "SẠCH SAU ĐẤT 1KG Công Nghệ Mới", "original_price": "286.868₫"},
        ]}
        self.engine = ReplyEngine(
            responder=FakeResponder(),
            memory=CustomerMemory(root / "m.sqlite3", root / "j", session_id="s"),
            knowledge=self.knowledge, catalog_store=catalog,
            missing_reporter=MissingReporter(),
            raw_stream=RawStreamRecorder(root / "raw", enabled=False),
        )

    def tearDown(self):
        self.engine.close()
        self.temp.cleanup()

    def test_state_flags_unmapped_products_in_the_pin_schedule(self):
        # "Trong lịch ghim" = có trong timeline của một video, không phải bảng cứng.
        self.engine.video_timelines = lambda: {"timelines": {"v1": {
            "products": {"a": {"product_id": "p1"}, "b": {"product_id": "p5"}},
            "events": [{"product": "a"}, {"product": "b"}]}}}
        state = self.engine.mapping_state()
        by_id = {item["product_id"]: item for item in state["products"]}
        self.assertEqual(by_id["p1"]["profile_id"], "SP-03")
        self.assertTrue(by_id["p1"]["in_pin_schedule"])
        self.assertEqual(by_id["p5"]["profile_id"], "")
        self.assertTrue(by_id["p5"]["in_pin_schedule"])

    def test_state_exposes_only_cached_local_image_urls(self):
        # Ảnh đi qua cache máy: không có file cache thì không trả URL, không hotlink.
        self.engine.product_images.prefetch = lambda sources: 0
        raw = Path(self.temp.name) / "no_raw.json"
        with mock.patch.object(server_module, "LIVE_RAW_CATALOG_PATH", raw):
            state = self.engine.mapping_state()
            by_id = {item["product_id"]: item for item in state["products"]}
            self.assertEqual(by_id["p1"]["image_url"], "")
            cache = self.engine.product_images.root
            cache.mkdir(parents=True, exist_ok=True)
            (cache / "p1.jpg").write_bytes(b"fake-jpeg")
            state = self.engine.mapping_state()
            by_id = {item["product_id"]: item for item in state["products"]}
            self.assertRegex(by_id["p1"]["image_url"], r"^/api/product/thumb/p1\?v=\d+$")
            self.assertEqual(by_id["p5"]["image_url"], "")

    def test_profile_image_is_served_from_cache_and_only_when_featured(self):
        # Hồ sơ có featured_image_url: URL local, file tải về nằm trong cache máy.
        saved = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        saved["nhom_san_pham"][0]["san_pham"][0]["featured_image_url"] = "https://example.com/kich.jpg"
        self.catalog_path.write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
        self.engine.product_images.prefetch = lambda sources: 0
        with mock.patch.object(server_module, "LIVE_RAW_CATALOG_PATH", Path(self.temp.name) / "no_raw.json"):
            state = self.engine.mapping_state()
            profiles = {p["id"]: p for p in state["profiles"]}
            self.assertEqual(profiles["SP-03"]["image_url"].split("?")[0], "/api/profile/thumb/SP-03")
            self.assertEqual(profiles["SP-09"]["image_url"], "")
            with self.assertRaises(ValueError):
                self.engine.profile_thumb_path("SP-03")
            cache = self.engine.product_images.root
            cache.mkdir(parents=True, exist_ok=True)
            (cache / "profile-SP-03.jpg").write_bytes(b"fake-jpeg")
            self.assertEqual(self.engine.profile_thumb_path("SP-03"), cache / "profile-SP-03.jpg")
            with self.assertRaises(ValueError):
                self.engine.profile_thumb_path("SP-09")

    def test_saving_a_mapping_approves_the_profile_and_reloads_live(self):
        self.assertIsNone(self.knowledge.match("SẠCH SAU ĐẤT 1KG", product_id="p5"))
        result = self.engine.save_mapping({"assignments": {"p5": "SP-09"}})
        self.assertTrue(result["ok"])
        self.assertEqual(result["changed"], 1)
        # Không khởi động lại mà hồ sơ đã dùng được ngay.
        matched = self.knowledge.match("SẠCH SAU ĐẤT 1KG", product_id="p5")
        self.assertEqual(matched["ten_san_pham"], "Sạch Sâu Đất")
        saved = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        profile = saved["nhom_san_pham"][0]["san_pham"][1]
        self.assertTrue(profile["approved_for_live"])
        self.assertIn("approved_at", profile)

    def test_reassigning_moves_the_sku_and_never_leaves_it_in_two_profiles(self):
        self.engine.save_mapping({"assignments": {"p1": "SP-09"}})
        saved = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        old, new = saved["nhom_san_pham"][0]["san_pham"]
        self.assertEqual(old["shop_product_ids"], [])
        self.assertFalse(old["approved_for_live"])
        self.assertEqual(new["shop_product_ids"], ["p1"])

    def test_unassigning_makes_the_system_silent_again(self):
        self.engine.save_mapping({"assignments": {"p5": "SP-09"}})
        self.engine.save_mapping({"assignments": {"p5": ""}})
        self.assertIsNone(self.knowledge.match("SẠCH SAU ĐẤT 1KG", product_id="p5"))

    def test_bad_payload_is_rejected(self):
        self.assertFalse(self.engine.save_mapping({})["ok"])


class ProfileInheritanceTests(unittest.TestCase):
    """Hồ sơ 1L khai cùng công thức với 5L — trước đây trường này bị bỏ quên."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        catalog = root / "knowledge.json"
        catalog.write_text(json.dumps({"nhom_san_pham": [{"san_pham": [
            {"san_pham_id": "SP-13", "ten_san_pham": "Đạm cá 5L",
             "approved_for_live": True, "shop_product_ids": ["p5"]},
            {"san_pham_id": "SP-14", "ten_san_pham": "Đạm cá 1L",
             "approved_for_live": True, "shop_product_ids": ["p1"]},
        ]}]}, ensure_ascii=False), encoding="utf-8")
        profiles = root / "profiles.json"
        profiles.write_text(json.dumps({"products": {
            "SP-13": {"technical_profile": {"Amino acid": "10%"},
                      "directions": ["Cây con: 30–50 ml cho 20 lít nước."],
                      "package": "Can 5 lít",
                      "promotion": "Mua 1 can tặng 1 gói Nano Lân"},
            "SP-14": {"inherits_technical_profile_from": "SP-13",
                      "package": "Chai 1 lít",
                      "promotion": "Mua 1 chai tặng 1 chai TC01"},
        }}, ensure_ascii=False), encoding="utf-8")
        self.knowledge = KnowledgeBase(catalog, profiles)

    def tearDown(self):
        self.temp.cleanup()

    def test_formula_fields_are_inherited(self):
        record = self.knowledge.match("", product_id="p1")
        profile = self.knowledge.context(record)["ho_so_da_duyet"]
        self.assertEqual(profile["technical_profile"], {"Amino acid": "10%"})
        self.assertEqual(profile["directions"], ["Cây con: 30–50 ml cho 20 lít nước."])
        self.assertEqual(profile["inherited_from"], "SP-13")

    def test_commercial_fields_are_never_inherited(self):
        profile = self.knowledge.context(
            self.knowledge.match("", product_id="p1"))["ho_so_da_duyet"]
        # Quy cách và khuyến mãi của chai 1L phải giữ nguyên của chính nó.
        self.assertEqual(profile["package"], "Chai 1 lít")
        self.assertEqual(profile["promotion"], "Mua 1 chai tặng 1 chai TC01")

    def test_missing_or_circular_parent_is_survivable(self):
        self.knowledge.profiles["SP-14"]["inherits_technical_profile_from"] = "SP-99"
        self.assertNotIn("inherited_from", self.knowledge.context(
            self.knowledge.match("", product_id="p1"))["ho_so_da_duyet"])
        self.knowledge.profiles["SP-14"]["inherits_technical_profile_from"] = "SP-13"
        self.knowledge.profiles["SP-13"]["inherits_technical_profile_from"] = "SP-14"
        profile = self.knowledge.context(
            self.knowledge.match("", product_id="p1"))["ho_so_da_duyet"]
        self.assertEqual(profile["package"], "Chai 1 lít")


class RawStreamDedupTests(unittest.TestCase):
    def test_same_message_is_never_written_twice(self):
        temp = tempfile.TemporaryDirectory()
        recorder = RawStreamRecorder(Path(temp.name), enabled=True)
        row = {"text": "giá bao nhiêu", "msg_id": "m1", "user_id": "u1"}
        self.assertEqual(recorder.record([row]), 1)
        self.assertEqual(recorder.record([row]), 0)
        self.assertEqual(recorder.duplicates, 1)
        # Dòng không có msg_id (câu AI tự ghi) vẫn phải được ghi.
        self.assertEqual(recorder.record([{"text": "@u1 289k nha", "is_host": True}]), 1)
        self.assertEqual(recorder.record([{"text": "@u1 289k nha", "is_host": True}]), 1)
        temp.cleanup()

    def test_dedup_and_count_survive_backend_restart(self):
        temp = tempfile.TemporaryDirectory()
        directory = Path(temp.name)
        row = {"text": "giá bao nhiêu", "msg_id": "restart-m1", "user_id": "u1"}
        first = RawStreamRecorder(directory, enabled=True)
        self.assertEqual(first.record([row]), 1)
        restarted = RawStreamRecorder(directory, enabled=True)
        self.assertEqual(restarted.count, 1)
        self.assertEqual(restarted.record([row]), 0)
        self.assertEqual(restarted.duplicates, 1)
        temp.cleanup()

    def test_duplicate_inside_one_batch_is_written_once(self):
        temp = tempfile.TemporaryDirectory()
        recorder = RawStreamRecorder(Path(temp.name), enabled=True)
        row = {"text": "cùng một câu", "msg_id": "batch-m1", "user_id": "u1"}
        self.assertEqual(recorder.record([row, dict(row)]), 1)
        self.assertEqual(recorder.duplicates, 1)
        temp.cleanup()

    def test_failed_disk_write_does_not_poison_dedup_state(self):
        temp = tempfile.TemporaryDirectory()
        recorder = RawStreamRecorder(Path(temp.name), enabled=True)
        row = {"text": "không được mất", "msg_id": "disk-fail-m1"}
        with mock.patch.object(Path, "open", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                recorder.record([row])
        self.assertEqual(recorder.record([row]), 1)
        self.assertEqual(recorder.duplicates, 0)
        temp.cleanup()


class DoseScalingTests(unittest.TestCase):
    """Hai sản phẩm ghim nhiều nhất từng không quy đổi được liều."""

    TC01 = {"dose_extrapolation_rule": "nearest_endpoint", "huong_dan_su_dung": [
        "Cây cảnh, bonsai: pha 10–15ml cho bình 16 lít, dùng 7–10 ngày/lần"]}
    FISH_1L = {
        "huong_dan_su_dung": ["Dùng theo hướng dẫn của cùng công thức 5L"],
        "ho_so_da_duyet": {"directions": [
            "Cây con: 30–50 ml cho 20 lít nước.",
            "Trước ra hoa: 70–100 ml cho 20 lít nước."]},
    }
    TOP_ONE = {"dose_extrapolation_rule": "nearest_endpoint",
               "huong_dan_su_dung": ["Pha 240ml cho phuy 200 lít nước"]}

    def test_tank_word_between_cho_and_the_number_is_understood(self):
        # "cho bình 16 lít" là cách ghi phổ biến nhất, bản cũ bỏ sót hoàn toàn.
        rows = verified_dose_calculations("bình 16 lít pha bao nhiêu", self.TC01)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["calculated_ml_min"], 10)
        self.assertEqual(rows[0]["calculated_ml_max"], 15)

    def test_outside_range_does_not_infer_without_enabled_policy(self):
        rows = verified_dose_calculations("pha cho bình 32 lít",
            {"huong_dan_su_dung": self.TC01["huong_dan_su_dung"]})
        self.assertEqual(rows, [])

    def test_inherited_directions_make_the_one_litre_bottle_answerable(self):
        rows = verified_dose_calculations("bình 20 lít pha nhiêu ml", self.FISH_1L)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["calculated_ml_min"], 30)

    def test_identical_ratios_written_twice_are_not_duplicated(self):
        approved = {
            "dose_extrapolation_rule": "nearest_endpoint",
            "huong_dan_su_dung": ["Pha 240ml cho phuy 200 lít nước"],
            "ho_so_da_duyet": {"directions": ["Pha 240 ml cho phuy 200 lít nước."]},
        }
        self.assertEqual(len(server_module._dose_processor.approved_directions(approved)), 2)
        rows = verified_dose_calculations("bình 25l pha bao nhiêu", approved)
        self.assertEqual(len(rows), 1, "cùng một tỷ lệ không được thành hai liều")
        self.assertEqual(rows[0]["calculated_ml"], 30)

    def test_bare_litres_and_other_containers_are_understood(self):
        for question in ("pha cho 16 lít nước", "16 lit pha bao nhieu",
                         "thùng 16 lít pha sao"):
            self.assertTrue(
                verified_dose_calculations(question, self.TOP_ONE), question)

    def test_bottle_size_never_hijacks_the_spray_tank_size(self):
        # "đạm cá 1L pha cho bình 20 lít": 1L là chai, 20 lít mới là bình.
        self.assertEqual(
            server_module._dose_processor.target_tank_liters("đạm cá 1l pha bao nhiêu cho bình 20 lít"), 20)
        self.assertEqual(server_module._dose_processor.target_tank_liters("đạm cá 1l pha cho 20 lít"), 20)
        self.assertEqual(server_module._dose_processor.target_tank_liters("bình 16 lít pha bao nhiêu"), 16)
        self.assertEqual(server_module._dose_processor.target_tank_liters("16 lit pha bao nhieu"), 16)
        self.assertEqual(server_module._dose_processor.target_tank_liters("pha sao"), 0)
        rows = verified_dose_calculations(
            "đạm cá 1l pha bao nhiêu cho bình 20 lít", self.FISH_1L)
        self.assertEqual(rows[0]["calculated_ml_min"], 30)
        self.assertEqual(rows[0]["calculated_ml_max"], 50)

    def test_powder_doses_in_grams_are_read_with_the_right_unit(self):
        # Rydoxin, Jialeton, A6, Magie Kẽm... đều ghi liều bằng gam. Bản cũ
        # chỉ bắt "ml" nên mọi hàng dạng bột không có phép tính nào, tức là
        # model tự bịa liều thuốc trừ nấm.
        approved = {"huong_dan_su_dung": ["Pha 30g/bình 20-25 lít nước"]}
        rows = verified_dose_calculations("bình 25 lít pha bao nhiêu", approved)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["don_vi"], "g")
        self.assertEqual(rows[0]["calculated_ml_max"], 30)
        self.assertIn("30g", rows[0]["calculation"])

    def test_dose_written_tank_first_is_understood(self):
        # "Bình 16 lít: 25 – 30ml" — nhãn Nano Tonic viết ngược, bản cũ bỏ sót.
        approved = {"huong_dan_su_dung": ["Bình 16 lít: 25 – 30ml"]}
        rows = verified_dose_calculations("bình 16 lít pha bao nhiêu", approved)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["calculated_ml_min"], rows[0]["calculated_ml_max"]),
                         (25, 30))
        self.assertEqual(rows[0]["don_vi"], "ml")

    def test_a_bottle_size_before_litres_is_not_read_as_a_tank(self):
        # Kieu nguoc doi phai co tu chi binh, neu khong "chai 100ml pha 400
        # lit" se bi doc thanh "100 lit cho 400ml".
        approved = {"dose_extrapolation_rule": "nearest_endpoint",
                    "huong_dan_su_dung": ["chai 100ml pha được 400 lít nước"]}
        rows = verified_dose_calculations("bình 20 lít pha bao nhiêu", approved)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source_tank_liters_max"], 400)
        self.assertEqual(rows[0]["calculated_ml_max"], 5)

    def test_kilogram_doses_become_grams_before_rounding(self):
        # "1kg pha 1500 lít" để nguyên kg thì bình 20L ra 0.013kg, làm tròn
        # 2 số còn 0.01kg — sai 25% và không ai đong được ngoài vườn.
        approved = {"dose_extrapolation_rule": "nearest_endpoint",
                    "huong_dan_su_dung": ["Hòa 1kg với 1500 lít nước"]}
        rows = verified_dose_calculations("bình 20 lít pha bao nhiêu", approved)
        self.assertEqual(rows[0]["don_vi"], "g")
        self.assertEqual(rows[0]["calculated_ml_max"], 13.33)

    def test_combo_gives_each_component_its_own_dose_never_a_sum(self):
        # Chủ dự án xác nhận 11/09/2026: đánh chung một bình, mỗi loại giữ
        # nguyên liều của chính nó. Bản cũ trả [] nên model tự tính.
        combo = {
            "is_combo": True,
            "combo_components": [
                {"ten_san_pham": "NANO ĐỒNG",
                 "dose_extrapolation_rule": "nearest_endpoint",
                 "huong_dan_su_dung": ["Pha 250ml với khoảng 100-200 lít nước"]},
                {"ten_san_pham": "RYDOXIN",
                 "huong_dan_su_dung": ["Pha 30g/bình 20-25 lít nước"]},
            ],
        }
        rows = verified_dose_calculations("bình 25 lít pha bao nhiêu", combo)
        by_name = {row["combo_component"]: row for row in rows}
        self.assertEqual(set(by_name), {"NANO ĐỒNG", "RYDOXIN"})
        self.assertEqual(by_name["RYDOXIN"]["calculated_ml_max"], 30)
        self.assertEqual(by_name["RYDOXIN"]["don_vi"], "g")
        self.assertEqual(by_name["NANO ĐỒNG"]["calculated_ml_max"], 62.5)
        self.assertTrue(all(row["combo_component"] in row["calculation"]
                            for row in rows))

    def test_a_package_size_is_never_read_as_a_price_the_shopper_reported(self):
        # "đạm cá can 5 lít giá nhiêu" từng bị đọc thành "khách báo giá 5đ",
        # rồi AI trả lời "giá 5 ngon quá, tài khoản anh đang có mã rồi đó".
        from server import customer_price_signal
        product = {"original_price": "555.000₫", "sale_price": "555.000₫"}
        self.assertEqual(
            customer_price_signal("đạm cá can 5 lít giá nhiêu", product), {})
        self.assertEqual(
            customer_price_signal("chai 500ml giá bao nhiêu", product), {})
        # Giá khách thật sự báo thì vẫn phải bắt được.
        signal = customer_price_signal("em thấy giá 226k à", product)
        self.assertEqual(signal.get("reported_price_vnd"), 226_000)

    def test_a_shopper_who_already_bought_is_not_pushed_to_buy_again(self):
        # 12/09 LIVE: "vừa nhận 1 lọ TC 01 giá 90k mà giờ vào xem lại giá 77k
        # là sao nhỉ" -> AI trả lời "77k là giá sau mã tài khoản anh rồi đó,
        # ngon quá, chốt liền đi nha". Khách ĐÃ MUA và đang khó chịu.
        from server import customer_price_signal
        product = {"original_price": "120.000₫", "sale_price": "90.000₫"}
        signal = customer_price_signal(
            "vừa nhận 1 lọ TC 01 giá 90k mà giờ vào xem lại giá 77k là sao nhỉ",
            product)
        self.assertTrue(signal.get("da_mua_roi"))
        self.assertIn("ĐÃ MUA RỒI", signal["interpretation"])
        self.assertIn("không thúc khách đặt thêm", signal["response_goal"].casefold())
        # Khách đang cân nhắc mua thì KHÔNG được gắn nhãn đã mua.
        thuong = customer_price_signal("em thấy app có 90k à", product)
        self.assertFalse(thuong.get("da_mua_roi"))

    def test_shop_info_is_loaded_so_the_address_question_is_answerable(self):
        from server import load_shop_info
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "shop.json"
            path.write_text(json.dumps({
                "dia_chi_mua_hang": "Địa chỉ mẫu",
                "hotline": "0123456789",
                "_ghi_chu_noi_bo": "Không đưa vào prompt",
            }), encoding="utf-8")
            info = load_shop_info(path)
        self.assertTrue(info.get("dia_chi_mua_hang"), "phải có địa chỉ shop")
        self.assertTrue(info.get("hotline"))
        # Khoá ghi chú nội bộ lại, không đẩy vào prompt.
        self.assertFalse([k for k in info if k.startswith("_")])
        # Thiếu file thì im lặng chứ không vỡ.
        self.assertEqual(load_shop_info("khong-ton-tai.json"), {})

    def test_claiming_the_shopper_already_has_a_voucher_is_detected(self):
        from server import asserts_voucher
        self.assertTrue(asserts_voucher("Tài khoản anh đang có mã rồi đó"))
        self.assertTrue(asserts_voucher("ai cũng có mã hết á"))
        # Mời khách tự kiểm tra thì không sao — đây là câu chủ dự án muốn.
        self.assertFalse(asserts_voucher(
            "Gốc 289k đó anh, nay TikTok hỗ trợ mã lớn lắm, anh xem tài khoản "
            "có hông rồi đặt liền nha"))

    def test_a_price_question_never_produces_a_dose(self):
        self.assertEqual(verified_dose_calculations("giá bao nhiêu", self.TC01), [])

    def test_package_size_in_a_price_question_is_not_a_spray_tank(self):
        # "can 5 lít" ở đây là quy cách đóng gói, không phải bình phun.
        self.assertEqual(
            verified_dose_calculations(
                "đạm cá can 5 lít giá bao nhiêu", self.TC01, dose_intent=False),
            [],
        )

    def test_dose_intent_is_answerable_when_only_the_profile_has_directions(self):
        missing = KnowledgeBase.missing_fields(
            "bình 20 lít pha bao nhiêu", {"original_price": "1₫"},
            {"ho_so_da_duyet": {"directions": ["30 ml cho 20 lít nước."]}},
        )
        self.assertNotIn("lieu_dung", missing)


class VideoPositionTests(unittest.TestCase):
    """Video phat lai khong bao gio bat dau tu 00:00 nen phai nghe ra vi tri."""

    def setUp(self):
        self.store = VideoPositionStore(max_age_s=5.0)

    def test_nothing_measured_yet_is_reported_as_unavailable(self):
        state = self.store.current()
        self.assertFalse(state["available"])
        self.assertEqual(state["reason"], "no_measurement")

    def test_position_advances_with_real_time_after_the_measurement(self):
        self.store.update({"position_s": 100.0, "margin": 0.3,
                           "video_duration_s": 1140.77})
        self.store._state["monotonic"] -= 4.0
        state = self.store.current()
        self.assertTrue(state["available"])
        self.assertAlmostEqual(state["position_s"], 104.0, places=1)

    def test_position_wraps_at_the_end_of_the_video(self):
        self.store.update({"position_s": 1139.0, "margin": 0.3,
                           "video_duration_s": 1140.77})
        self.store._state["monotonic"] -= 4.0
        self.assertAlmostEqual(self.store.current()["position_s"], 2.23, places=1)

    def test_a_stale_measurement_is_never_trusted(self):
        self.store.update({"position_s": 10.0, "margin": 0.3})
        self.store._state["monotonic"] -= 60.0
        state = self.store.current()
        self.assertFalse(state["available"])
        self.assertEqual(state["reason"], "stale")

    def test_bad_payloads_are_refused(self):
        self.assertFalse(self.store.update({})["ok"])
        self.assertFalse(self.store.update({"position_s": "abc"})["ok"])
        self.assertFalse(self.store.update({"position_s": -1})["ok"])
        self.assertFalse(self.store.update({"position_s": float('nan')})["ok"])
        self.assertFalse(self.store.update({"position_s": float('inf')})["ok"])
        self.assertTrue(self.store.update({"position_s": 1, "margin": float('nan')})["ok"])
        self.assertEqual(self.store.current()["margin"], 0)


class VideoLibraryTests(unittest.TestCase):
    """Lịch ghim là dữ liệu của từng video, không còn nằm trong schedule.js."""

    GOOD = {
        "schema_version": 1, "video_id": "live9", "title": "Video 9",
        "cycle_ms": 300000, "repin_every_ms": 15000,
        "products": {
            "a": {"bag_index": 1, "short_name": "A", "include_all": ["a"]},
            "b": {"bag_index": 2, "short_name": "B", "include_all": ["b"]},
        },
        "events": [{"at_ms": 0, "product": "a"},
                   {"at_ms": 120000, "product": "b"},
                   {"at_ms": 240000, "product": None}],
        "script_contexts": [{"at_ms": 0, "topic": "A", "facts": []}],
    }

    def test_a_good_timeline_passes(self):
        from video_library import validate_timeline
        self.assertEqual(validate_timeline(self.GOOD), [])

    def test_every_broken_shape_is_caught(self):
        from video_library import validate_timeline
        cases = {
            "thiếu hết": {},
            "cycle_ms 0": {**self.GOOD, "cycle_ms": 0},
            "video_id rỗng": {**self.GOOD, "video_id": ""},
            "products rỗng": {**self.GOOD, "products": {}},
            "events rỗng": {**self.GOOD, "events": []},
            "mốc đầu khác 0": {**self.GOOD,
                               "events": [{"at_ms": 5000, "product": "a"}]},
            "product lạ": {**self.GOOD,
                           "events": [{"at_ms": 0, "product": "z"}]},
            "at_ms không tăng": {**self.GOOD, "events": [
                {"at_ms": 0, "product": "a"}, {"at_ms": 0, "product": "b"}]},
            "at_ms vượt chu kỳ": {**self.GOOD, "events": [
                {"at_ms": 0, "product": "a"}, {"at_ms": 999999, "product": "b"}]},
            "thiếu alternate_every_ms": {**self.GOOD, "events": [
                {"at_ms": 0, "product": "a", "alternate_with": "b"}]},
            "include_all rỗng": {**self.GOOD, "products": {
                "a": {"bag_index": 1, "include_all": []}}},
        }
        for label, timeline in cases.items():
            with self.subTest(label):
                self.assertTrue(validate_timeline(timeline), label)

    def test_the_shipped_live1_timeline_is_valid(self):
        from _du_lieu_may import can_file
        from video_library import validate_timeline
        path = can_file("videos/live1/timeline.json") / "videos" / "live1" / "timeline.json"
        timeline = json.loads(path.read_text(encoding="utf-8-sig"))
        self.assertEqual(validate_timeline(timeline), [])
        self.assertEqual(timeline["video_id"], "live1")

    def test_engine_serves_only_valid_timelines(self):
        temp = tempfile.TemporaryDirectory()
        import video_library
        original = video_library.VIDEO_DIR
        try:
            video_library.VIDEO_DIR = Path(temp.name)
            (Path(temp.name) / "live9").mkdir()
            (Path(temp.name) / "live9" / "timeline.json").write_text(
                json.dumps(self.GOOD, ensure_ascii=False), encoding="utf-8")
            (Path(temp.name) / "hong").mkdir()
            (Path(temp.name) / "hong" / "timeline.json").write_text(
                '{"video_id": "hong"}', encoding="utf-8")
            listing = {item["video_id"]: item["errors"]
                       for item in video_library.list_videos(check_fingerprint=False)}
            self.assertEqual(listing["live9"], [])
            self.assertTrue(listing["hong"])
        finally:
            video_library.VIDEO_DIR = original
            temp.cleanup()

    def test_video_id_must_match_its_folder_name(self):
        temp = tempfile.TemporaryDirectory()
        import video_library
        original = video_library.VIDEO_DIR
        try:
            video_library.VIDEO_DIR = Path(temp.name)
            (Path(temp.name) / "live9").mkdir()
            (Path(temp.name) / "live9" / "timeline.json").write_text(
                json.dumps({**self.GOOD, "video_id": "khac"}, ensure_ascii=False),
                encoding="utf-8")
            errors = video_library.list_videos(check_fingerprint=False)[0]["errors"]
            self.assertTrue(any("khác tên thư mục" in item for item in errors))
        finally:
            video_library.VIDEO_DIR = original
            temp.cleanup()

    def test_position_carries_the_detected_video_id(self):
        store = VideoPositionStore()
        store.update({"video_id": "live9", "position_s": 10, "margin": 0.3,
                      "video_duration_s": 300})
        self.assertEqual(store.current()["video_id"], "live9")


class SoftGapUnitTests(unittest.TestCase):
    def test_hard_gate_only_for_price_and_promotion(self):
        from knowledge import KnowledgeBase, general_knowledge_hint
        self.assertEqual(KnowledgeBase.missing_fields("pha chung được không", {}, {}), [])
        self.assertEqual(KnowledgeBase.missing_fields("giá bao nhiêu", {}, {}), ["gia_goc"])
        self.assertIn("pha_chung", KnowledgeBase.general_knowledge_gaps("pha chung được không", {}, {}))
        self.assertIsNone(general_knowledge_hint([]))
        self.assertIn("liều", general_knowledge_hint(["lieu_dung"]))

    def test_profile_with_mixing_text_has_no_gap(self):
        from knowledge import KnowledgeBase
        profile = {"huong_dan_su_dung": "Có thể pha chung với humic", "luu_y": ""}
        self.assertNotIn("pha_chung", KnowledgeBase.general_knowledge_gaps("pha chung được không", {}, profile))

    def test_recent_duplicate_within_three_minutes(self):
        import customer_card
        from datetime import datetime, timedelta
        now = datetime.now().astimezone()
        turn = lambda secs: {"comment": "Giá bao nhiêu vậy?", "response": "Gốc 289k nha", "delivery_status": "sent",
                             "happened_at": (now - timedelta(seconds=secs)).isoformat()}
        self.assertTrue(customer_card.recent_duplicate([turn(60)], "giá bao nhiêu vậy", now=now))
        self.assertFalse(customer_card.recent_duplicate([turn(400)], "giá bao nhiêu vậy", now=now))
        self.assertFalse(customer_card.recent_duplicate([{**turn(60), "response": ""}], "giá bao nhiêu vậy", now=now))

    def test_promo_pitch_only_once(self):
        import sales_playbook
        sent = [{"delivery_status": "sent", "response": "Gốc 289k đó, nay TikTok hỗ trợ mã lớn lắm"}]
        ctx = sales_playbook.add_card_and_hint({}, None, ["đạm cá giá sao"], turns=sent)
        self.assertIn("da_nhac_ma", ctx)
        self.assertNotIn("da_nhac_ma", sales_playbook.add_card_and_hint({}, None, ["giá"], turns=[]))

