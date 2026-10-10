from privacy import fold_identifiers, row_mentions


def test_short_common_names_are_not_identifiers():
    assert fold_identifiers(['Mai', 'Lan', 'Nguyễn', 'abc']) == []


def test_ids_and_multiword_names_are_identifiers():
    idents = fold_identifiers(['7692013951', 'Mai Anh Shop', 'user_abc123'])
    assert idents == ['7692013951', 'mai anh shop', 'user_abc123']


def test_whole_word_match_only():
    idents = fold_identifiers(['Mai Anh Shop', '7692013951'])
    assert not row_mentions({'text': 'ngày mai anh shopee giao hàng'}, idents)
    assert not row_mentions({'text': 'mã 976920139512'}, idents)
    assert row_mentions({'text': 'Khách Mai Anh Shop hỏi giá'}, idents)
    assert row_mentions({'payload': '{"user_id": "7692013951"}'}, idents)
