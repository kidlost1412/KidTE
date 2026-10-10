"""B06: facts a reply depends on are re-checked at real send time; one recompose at most."""
import copy
import json
import pathlib
import sqlite3
import tempfile
import time
import unittest
import unittest.mock
from types import SimpleNamespace

import fact_revision
from delivery_store import DeliveryStore
from shopee_ai import ShopeeAiEngine
import test_server as ts

ROOM = 'room-1'


class FactRevisionServerTests(unittest.TestCase):
    setUp = ts.StandaloneServerTests.setUp
    tearDown = ts.StandaloneServerTests.tearDown
    _sync = ts.StandaloneServerTests._sync

    def prepare(self, check=True):
        patcher = unittest.mock.patch.dict('os.environ', {'KIDTE_FACT_REVISION_CHECK': '1' if check else '0'})
        patcher.start()
        self.addCleanup(patcher.stop)
        real_sync = self.catalog.sync
        # Like the real store: a snapshot without products never wipes the catalog.
        self.catalog.sync = lambda snap: (self.catalog.snapshot if not (snap or {}).get('products')
                                          and self.catalog.snapshot else real_sync(snap))
        self.engine.heartbeat({'platform': 'tiktok', 'session_id': ROOM,
            'live_running': True, 'ai_enabled': True, 'ai_ready': True})

    def ask(self, text, cid, **extra):
        payload = {**ts.comment(text, cid), 'protocol_version': 2, **extra}
        return self.engine.queued_reply('tiktok', payload)

    def claim(self, answer):
        return self.engine.delivery_operation('tiktok', 'claim', {
            'turn_id': answer['turn_id'], 'room_id': ROOM, 'message_id': answer['messages'][0]['message_id']})

    def set_price(self, price):
        snap = copy.deepcopy(self.catalog.snapshot)
        snap['products'][0]['original_price'] = price
        self.catalog.snapshot = snap

    def next_turn(self):
        return self.engine.queue_operation('tiktok', 'next', {'room_id': ROOM})['result']

    def test_check_off_by_default_keeps_legacy_send_path(self):
        self.prepare(check=False)
        first = self.ask('giá bao nhiêu', 'p1')
        self.set_price('150.000₫')
        claimed = self.claim(first)
        self.assertTrue(claimed['ok'], claimed)

    def test_price_change_recomposes_exactly_once_then_second_change_cancels(self):
        self.prepare()
        first = self.ask('giá bao nhiêu', 'p1')
        stored = json.loads(self.engine.delivery_store.get(first['turn_id'])['fact_revision'])
        self.assertEqual(stored['product_ids'], ['p1'])
        calls = len(self.responder.calls)
        self.set_price('150.000₫')
        changed = self.claim(first)
        self.assertEqual((changed['ok'], changed['error']), (False, 'revision_changed'))
        self.assertEqual(len(self.responder.calls), calls + 1)
        old = self.engine.delivery_store.get(first['turn_id'])
        self.assertEqual((old['state'], old['messages'][0]['dispatched_at']), ('cancelled', None))
        second = self.next_turn()
        self.assertNotEqual(second['turn_id'], first['turn_id'])
        self.assertIn('150.000₫', json.dumps(self.responder.calls[-1][2], ensure_ascii=False))
        self.assertEqual(self.engine.delivery_store.get(second['turn_id'])['recompose_count'], 1)
        self.set_price('170.000₫')
        stale = self.claim(second)
        self.assertEqual(stale['error'], 'revision_stale')
        self.assertEqual(len(self.responder.calls), calls + 1)  # no second recompose
        cancelled = self.engine.delivery_store.get(second['turn_id'])
        self.assertEqual((cancelled['state'], cancelled['cancel_reason']), ('cancelled', 'revision_stale'))
        self.assertEqual(self.engine.comment_queue.by_turn(second['turn_id'])['state'], 'cancelled')

    def test_recomposed_turn_is_claimable_when_facts_stay_put(self):
        self.prepare()
        first = self.ask('giá bao nhiêu', 'p2')
        self.set_price('150.000₫')
        self.assertEqual(self.claim(first)['error'], 'revision_changed')
        self.assertTrue(self.claim(self.next_turn())['ok'])

    def test_unrelated_product_and_stock_changes_do_not_matter(self):
        self.prepare()
        first = self.ask('giá bao nhiêu', 'u1')
        snap = copy.deepcopy(self.catalog.snapshot)
        snap['products'][0]['stock_num'] = 3
        snap['products'].append({'product_id': 'p9', 'title': 'Khác', 'original_price': '1'})
        self.catalog.snapshot = snap
        self.assertTrue(self.claim(first)['ok'])

    def test_named_product_survives_video_change_but_unnamed_does_not(self):
        self.prepare()
        video = {'current_topic': 'Đang nói về rễ', 'scripted_product_id': 'p1'}
        self.engine.video_positions['tiktok'].update({'position_s': 5, 'lock_id': 'A', 'revision': 1})
        named = self.ask('TC01 dùng sao', 'v1', video_context=video)
        facts = json.loads(self.engine.delivery_store.get(named['turn_id'])['fact_revision'])
        self.assertEqual((facts['video_dependent'], facts['explicit'], facts['lock_id']), (True, True, 'A'))
        self.engine.video_positions['tiktok'].update({'position_s': 9, 'lock_id': 'B', 'revision': 2})
        claimed = self.claim(named)
        self.assertTrue(claimed['ok'], claimed)
        self.engine.comment_queue.settle(named['turn_id'], 'sent')
        unnamed = self.ask('dùng sao vậy', 'v2', video_context=video)
        self.engine.video_positions['tiktok'].update({'position_s': 12, 'lock_id': 'C', 'revision': 3})
        self.assertEqual(self.claim(unnamed)['error'], 'revision_changed')

    def test_pin_based_reply_is_cancelled_when_pin_changes(self):
        self.prepare()
        first = self.ask('giá bao nhiêu', 'pin1')
        facts = json.loads(self.engine.delivery_store.get(first['turn_id'])['fact_revision'])
        self.assertTrue(facts['pin_dependent'])
        snap = copy.deepcopy(self.catalog.snapshot)
        snap['pin_id'] = 'p2'
        self.catalog.snapshot = snap
        self.assertEqual(self.claim(first)['error'], 'revision_changed')
        self.assertEqual(self.engine.delivery_store.get(first['turn_id'])['state'], 'cancelled')

    def test_no_recompose_after_deadline(self):
        self.prepare()
        first = self.ask('giá bao nhiêu', 'late1')
        calls = len(self.responder.calls)
        self.set_price('150.000₫')
        self.engine.delivery_store.clock = lambda: time.time() + 130
        # The route reconciles first, so an expired turn is simply not claimable...
        self.assertFalse(self.claim(first)['ok'])
        self.assertEqual(len(self.responder.calls), calls)  # ...and never recomposed.

    def test_store_cancels_as_stale_when_deadline_passed_before_recompose(self):
        store = DeliveryStore(':memory:', clock=lambda: 1000.0)
        facts = fact_revision.build({'a': {'product_id': 'a', 'price': 1}}, product_ids=['a'])
        turn = store.prepare('t', 'tiktok', 'r', 'i', ['Tin'], expires_at=999.0, fact_revision=facts)
        changed = {'hash': 'other', 'pin_id': '', 'lock_id': '', 'revision': 0}
        claim = store.claim('t', turn['messages'][0]['message_id'], 'tiktok', 'r', current_facts=changed)
        self.assertEqual((claim['error'], claim['recompose']), ('revision_stale', False))
        self.assertEqual(store.get('t')['state'], 'cancelled')
        store.close()

    def test_legacy_turn_without_fact_revision_is_unaffected(self):
        self.prepare()
        first = self.ask('giá bao nhiêu', 'old1')
        with self.engine.delivery_store._db:
            self.engine.delivery_store._db.execute("UPDATE reply_turns SET fact_revision=''")
        self.set_price('150.000₫')
        self.assertTrue(self.claim(first)['ok'])


class FactRevisionStoreTests(unittest.TestCase):
    def test_old_database_is_migrated_and_legacy_prepare_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / 'm.sqlite3'
            db = sqlite3.connect(path)
            db.executescript("""CREATE TABLE reply_turns (turn_id TEXT PRIMARY KEY, platform TEXT NOT NULL,
                room_id TEXT NOT NULL, interaction_id TEXT NOT NULL, created_at REAL NOT NULL,
                expires_at REAL NOT NULL, state TEXT NOT NULL DEFAULT 'ready',
                source_dependent INTEGER NOT NULL DEFAULT 1);
                INSERT INTO reply_turns VALUES ('old','tiktok','r','i',1,9e12,'ready',1);""")
            db.commit()
            db.close()
            store = DeliveryStore(path)
            try:
                turn = store.get('old')
                self.assertEqual((turn['fact_revision'], turn['recompose_count']), ('', 0))
                store.prepare('new', 'tiktok', 'r', 'i2', ['Tin'])
                message = store.get('new')['messages'][0]
                # Legacy turns never see a revision verdict even if current facts are supplied.
                claim = store.claim('new', message['message_id'], 'tiktok', 'r',
                                    current_facts={'hash': 'x', 'pin_id': '', 'lock_id': 'z', 'revision': 9})
                self.assertTrue(claim['ok'])
            finally:
                store.close()

    def test_stale_reason_rules(self):
        base = fact_revision.build({'a': {'product_id': 'a', 'price': 1}}, product_ids=['a'], pin_id='a',
            pin_dependent=True, video_dependent=True, lock_id='L', revision=1)
        same = {'hash': base['hash'], 'pin_id': 'a', 'lock_id': 'L', 'revision': 1}
        self.assertEqual(fact_revision.stale_reason(base, same), '')
        self.assertEqual(fact_revision.stale_reason(base, {**same, 'hash': 'x'}), 'catalog')
        self.assertEqual(fact_revision.stale_reason(base, {**same, 'pin_id': 'b'}), 'pin')
        self.assertEqual(fact_revision.stale_reason(base, {**same, 'revision': 2}), 'video')
        self.assertEqual(fact_revision.stale_reason({**base, 'explicit': True}, {**same, 'revision': 2}), '')
        self.assertEqual(fact_revision.stale_reason(None, same), '')


class FactRevisionShopeeTests(unittest.TestCase):
    def test_shopee_turn_records_catalog_pin_and_recompose_flag(self):
        import test_shopee_ai as tsa
        catalog = tsa.priced_catalog()
        owner = SimpleNamespace(responder=tsa.FakeResponder(), knowledge=tsa.FakeKnowledge(),
            shopee_mapping_state=lambda: {'catalog': catalog, 'videos': {'v': {
                'mappings': {'tc01': {'shop_id': '10', 'product_id': '20'}}}}})
        with tempfile.TemporaryDirectory() as tmp:
            engine = ShopeeAiEngine(owner, pathlib.Path(tmp) / 'm.sqlite3', pathlib.Path(tmp) / 'j')
            try:
                payload = {'session_id': '123', 'video_id': 'v', 'comment_id': 'c1', 'user_id': '77',
                           'nickname': 'Mai', 'comment': 'TC01 giá bao nhiêu', 'shop_id': '10',
                           'product_id': '20', 'title': 'Kích Rễ TC01'}
                result = engine.reply(payload)
                turn = engine.delivery_store.get(result['turn_id'])
                facts = json.loads(turn['fact_revision'])
                self.assertEqual((facts['product_ids'], facts['pin_id'], turn['recompose_count']), (['20'], '20', 0))
                self.assertEqual(engine.latest_pin['123'], '20')
                before = facts['hash']
                catalog['products'][0]['price_data']['price'] = '99000'
                recomposed = engine.reply({**payload, '_recompose_count': 1})
                self.assertTrue(recomposed['should_send'])  # duplicate guard bypassed
                new = engine.delivery_store.get(recomposed['turn_id'])
                self.assertEqual(new['recompose_count'], 1)
                self.assertNotEqual(json.loads(new['fact_revision'])['hash'], before)
            finally:
                engine.close()
