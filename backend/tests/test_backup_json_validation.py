import pytest

from backup import BackupError, _strict_json_loads, _validate_json, _validate_jsonl


@pytest.mark.parametrize('payload', [
    '{"position":NaN}', '{"position":Infinity}', '{"position":-Infinity}',
    '{"nested":{"position":1e999}}', '{"id":"old","id":"new"}',
    '{"nested":{"dose":20,"dose":200}}',
])
def test_backup_rejects_ambiguous_or_nonfinite_json_and_jsonl(tmp_path, payload):
    with pytest.raises(ValueError):
        _strict_json_loads(payload)
    document = tmp_path / 'source.json'
    document.write_text(payload, encoding='utf-8')
    with pytest.raises(BackupError, match='JSON không hợp lệ'):
        _validate_json(document)
    stream = tmp_path / 'source.jsonl'
    stream.write_text('{"valid":1}\n\n' + payload + '\n', encoding='utf-8')
    with pytest.raises(BackupError, match='JSONL không hợp lệ'):
        _validate_jsonl(stream)


def test_backup_json_accepts_finite_numbers_and_distinct_nested_keys(tmp_path):
    payload = '{"dose":20,"nested":{"dose":2.5,"zero":0,"tiny":1e-200}}'
    document = tmp_path / 'source.json'
    document.write_text(payload, encoding='utf-8')
    _validate_json(document)
    assert _strict_json_loads(payload)['nested']['dose'] == 2.5
