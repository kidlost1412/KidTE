import time
from types import SimpleNamespace

import pytest
from shopee_ai import ShopeeAiEngine
from shopee_commerce import price_context, select_product, price_violation


def products():
    return [{"shop_id":"10","product_id":pid,"title":title,
             "price_data":{"currency":"VND","price":str(price),"price_before_discount":str(price)}}
            for pid,title,price in [('20','Kích Rễ TC01',120000),('21','Đạm Cá Amino 1L',289000),('22','Đạm Cá Amino 5L',559000)]]


def catalog():
    return {'session_id':'123','updated_at':time.time(),'products':products()}


def test_explicit_history_and_pinned_priorities():
    owner=SimpleNamespace()
    rows=products()
    turns=[{'comment':'đạm cá 5l giá bao nhiêu','product_title':rows[2]['title'],'delivery_status':'sent'}]
    assert select_product(owner,'giá nhiu em',rows,'20',turns)[0]['product_id']=='22'
    assert select_product(owner,'TC01 giá bao nhiêu',rows,'21',turns)[0]['product_id']=='20'
    assert select_product(owner,'chai này giá nhiêu',rows,'20',turns)[0]['product_id']=='20'
    assert select_product(owner,'giá nhiu em',rows,'20',[])[0]['product_id']=='20'
    assert select_product(owner,'đạm cá giá bao nhiêu',rows,'20',[])[0] is None


def test_promotion_words_do_not_switch_customer_topic_to_a_gift_listing():
    rows = products() + [
        {'product_id': '23', 'title': 'ĐẠM CÁ HỮU CƠ AMINO 1L - MUA 1 TẶNG 1'},
        {'product_id': '24', 'title': '(HÀNG TẶNG KÈM KHÔNG BÁN) KÍCH RỄ TC01'},
    ]
    turns = [{'comment': 'chai gì đó em', 'product_title': rows[0]['title'], 'delivery_status': 'sent'}]
    selected, reason = select_product(SimpleNamespace(), '10 chai tặng gì ko', rows, '21', turns)
    assert selected['product_id'] == '20'
    assert reason == 'customer_conversation'
    assert select_product(SimpleNamespace(), '10 chai đạm cá 5l tặng gì', rows, '20', turns)[0]['product_id'] == '22'


def test_container_breaks_root_stimulant_name_tie_without_becoming_sku_evidence():
    rows = [
        {'product_id': 'humic', 'title': 'Siêu Kích Rễ Humic'},
        {'product_id': 'tc01', 'title': 'Kích Rễ TC01 (chai 100ml)'},
        {'product_id': 'fish', 'title': 'Đạm Cá Amino (chai 1L)'},
    ]
    assert select_product(SimpleNamespace(), '10 chai kích rễ tặng gì', rows, 'humic', [])[0]['product_id'] == 'tc01'
    assert select_product(SimpleNamespace(), '10 chai tặng gì', rows, 'humic', [])[0]['product_id'] == 'humic'


@pytest.mark.parametrize('change',[{'session_id':'999'},{'updated_at':time.time()-100},{'updated_at':'bad'}, {'updated_at':None}, {'updated_at':float('nan')}])
def test_price_cannot_cross_session_or_use_stale_catalog(change):
    assert price_context(products()[0],{**catalog(),**change},'123')=={}


def test_original_price_unit_and_variant_range():
    row=products()[1]
    assert price_context(row,catalog(),'123')['quotable_price']=='289.000đ'
    row['price_data'].update(price='100000',price_before_discount='289000')
    assert price_context(row,catalog(),'123')['quotable_price_vnd']==289000
    row['price_data'].update(price_min_before_discount='289000',price_max_before_discount='559000')
    assert price_context(row,catalog(),'123')=={'needs_variant':True}
    row['price_data']['currency']='USD'
    assert price_context(row,catalog(),'123')=={}


def test_price_claims_are_checked_against_current_sku_and_customer_signal():
    owner=SimpleNamespace()
    price=price_context(products()[1],catalog(),'123')
    assert not price_violation(owner,'Can 1l giá gốc 289k nha',price,{})
    assert price_violation(owner,'Can 1l giá 559k nha',price,{})
    assert price_violation(owner,'Hai can giá 578k nha',price,{})
    assert price_violation(owner,'Bấm sản phẩm xem giá nha',price,{})
    assert not price_violation(owner,'185k là mức anh đang thấy nha',price,{'reported_price_vnd':185000})
    assert price_violation(owner,'Mình chốt liền nha',price,{'da_mua_roi':True})


def test_full_reply_keeps_customers_topic_after_pin_changes(tmp_path):
    captured=[]
    class Responder:
        def reply(self,nick,comment,context,max_chars,**kwargs):
            captured.append(context)
            return {'action':'reply','reply':'Giá gốc '+context['price']['quotable_price']+' nha'}
    knowledge=SimpleNamespace(match=lambda *args:None)
    owner=SimpleNamespace(responder=Responder(),knowledge=knowledge,
        shopee_mapping_state=lambda:{'videos':{'v':{'mappings':{'p':{'shop_id':'10','product_id':'20'}}}},'catalog':catalog()})
    engine=ShopeeAiEngine(owner,tmp_path/'memory.db',tmp_path/'journal')
    base={'session_id':'123','video_id':'v','user_id':'77','nickname':'Mai','shop_id':'10','product_id':'20','title':'Kích Rễ TC01'}
    try:
        first=engine.reply({**base,'comment_id':'1','comment':'đạm cá 5l giá bao nhiêu'})
        assert '559.000đ' in first['reply']
        engine.delivery({'session_id':'123','turn_id':first['turn_id'],'message_id':'receipt1'})
        second=engine.reply({**base,'comment_id':'2','comment':'giá nhiu em'})
        assert '559.000đ' in second['reply']
        assert captured[-1]['current_product']['product_id']=='22'
        # Different account with the same display name inherits no previous topic.
        other=engine.reply({**base,'user_id':'88','comment_id':'3','comment':'giá nhiu em'})
        assert '120.000đ' in other['reply']
    finally:
        engine.close()
