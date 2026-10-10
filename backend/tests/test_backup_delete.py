import json
import os
import subprocess

import pytest

from backup_delete import preview, execute, recover


def backup(root, name):
    target = root / name
    target.mkdir(parents=True)
    (target / 'manifest.json').write_text(json.dumps({'schema_version': 1}))
    (target / 'payload').write_bytes(b'123456')
    return target


def test_preview_and_delete_multiple_without_backup_of_backup(tmp_path):
    first = backup(tmp_path, 'one')
    second = backup(tmp_path, 'two')
    third = backup(tmp_path, 'keep')
    checked = preview(tmp_path, ['one', 'two', 'one'])
    assert checked['ids'] == ['one', 'two']
    assert checked['reclaimable_bytes'] == sum(p.stat().st_size for t in (first, second) for p in t.iterdir())
    result = execute(tmp_path, checked['ids'], checked['preview_token'])
    assert result['reclaimed_bytes'] == checked['reclaimable_bytes']
    assert list(tmp_path.iterdir()) == [third]


@pytest.mark.parametrize('ids', [[], '../elsewhere', ['../elsewhere'], ['C:\\data'], ['.deleting-1'], [False]])
def test_delete_rejects_arbitrary_paths(tmp_path, ids):
    with pytest.raises(ValueError, match='backup_ids_invalid'):
        preview(tmp_path, ids)


def test_preview_must_still_match_before_mutation(tmp_path):
    target = backup(tmp_path, 'one')
    checked = preview(tmp_path, ['one'])
    (target / 'payload').write_bytes(b'changed')
    with pytest.raises(ValueError, match='backup_preview_changed'):
        execute(tmp_path, ['one'], checked['preview_token'])
    assert target.is_dir()


def test_v2_objects_are_not_deleted_using_v1_rules(tmp_path):
    target = backup(tmp_path, 'one')
    (target / 'manifest.json').write_text('{"schema_version":2}')
    with pytest.raises(ValueError, match='schema_unsupported'):
        preview(tmp_path, ['one'])


def test_failed_removal_retains_deletion_journal(tmp_path, monkeypatch):
    backup(tmp_path, 'one')
    checked = preview(tmp_path, ['one'])
    def failure(path):
        raise PermissionError('locked')
    monkeypatch.setattr('backup_delete.shutil.rmtree', failure)
    with pytest.raises(PermissionError):
        execute(tmp_path, ['one'], checked['preview_token'])
    assert not (tmp_path / 'one').exists()
    assert len(list(tmp_path.glob('.delete-*.json'))) == 1
    assert len(list(tmp_path.glob('.deleting-*'))) == 1
    monkeypatch.undo()
    assert recover(tmp_path)['recovered_deleted_ids'] == ['one']
    assert list(tmp_path.iterdir()) == []


def test_recovery_refuses_forged_journal_paths(tmp_path):
    journal = tmp_path / ('.delete-' + 'a' * 32 + '.json')
    journal.write_text(json.dumps({'state': 'deleting', 'moves': [['../outside', '../outside']], 'tokens': {}}))
    with pytest.raises(ValueError, match='journal_unsafe'):
        recover(tmp_path)
    assert journal.exists()


def test_real_junction_or_symlink_is_rejected_before_traversal(tmp_path):
    root = tmp_path / 'backups'
    target = backup(root, 'one')
    outside = tmp_path / 'outside'
    outside.mkdir()
    sentinel = outside / 'keep'
    sentinel.write_text('must survive')
    link = target / 'outside-link'
    if os.name == 'nt':
        result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(outside)], capture_output=True)
        assert result.returncode == 0, result.stderr.decode(errors='replace')
    else:
        link.symlink_to(outside, target_is_directory=True)
    try:
        with pytest.raises(ValueError, match='backup_link_unsafe'):
            preview(root, ['one'])
        assert sentinel.read_text() == 'must survive'
    finally:
        if os.name == 'nt':
            os.rmdir(link)
        else:
            link.unlink()
