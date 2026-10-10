"""E04: journal coverage for profile overrides, product images, video copy and trash archive/restore moves."""
import base64
import io
import json
from unittest import mock

import pytest
from PIL import Image

from knowledge import KnowledgeBase
from workspace import WorkspaceStore


class Crash(BaseException):
    """Simulated process death: not caught by `except Exception` cleanup."""


class _Engine:
    def __init__(self, knowledge):
        self.knowledge = knowledge
        self.extension_heartbeat = {}
        self.video_analysis = type('V', (), {'root': None})()

    def mapping_state(self):
        return {"products": [{"product_id": "p1", "bag_index": 1}]}

    def video_analysis_jobs(self):
        return {"jobs": []}

    def current_video_position(self):
        return {"state": "waiting", "video_id": ""}


TIMELINE = {"video_id": "keep", "title": "Giữ", "source_file": "live 2.mp4", "cycle_ms": 60000,
            "repin_every_ms": 20000, "products": {"p": {"bag_index": 1, "include_all": ["p"]}},
            "events": [{"at_ms": 0, "product": "p"}], "script_contexts": []}


@pytest.fixture
def ws(tmp_path):
    cat, prof = tmp_path / 'k.json', tmp_path / 'p.json'
    cat.write_text('{"nhom_san_pham": []}', encoding='utf-8')
    prof.write_text('{"products":{}}', encoding='utf-8')
    videos = tmp_path / 'videos'
    for name in ('keep', 'other'):
        (videos / name).mkdir(parents=True)
        timeline = {**TIMELINE, 'video_id': name, 'title': name}
        if name == 'other':
            timeline['source_sha256'] = 'abc'
            timeline['source_file'] = 'live-2.mp4'
        (videos / name / 'timeline.json').write_text(json.dumps(timeline), encoding='utf-8')
        (videos / name / 'media.bin').write_bytes(b'MEDIA-' + name.encode())
    (videos / 'other' / 'transcript.json').write_text('{"t": 1}', encoding='utf-8')

    def listed(check_fingerprint=True):
        return [{"video_id": d.name, "timeline": json.loads((d / 'timeline.json').read_text(encoding='utf-8')),
                 "fingerprint": None, "errors": []} for d in sorted(videos.iterdir())
                if (d / 'timeline.json').exists()]
    engine = _Engine(KnowledgeBase(cat, prof))
    engine.knowledge.overrides_path = tmp_path / 'overrides.json'
    store = WorkspaceStore(engine, tmp_path)
    profile = {"id": "P1", "san_pham_id": "P1", "title": "P1", "ten_san_pham": "Goc", "revision": "r"}
    with mock.patch('workspace.VIDEO_DIR', videos), mock.patch('workspace.list_videos', side_effect=listed), \
            mock.patch.object(store, '_profiles', return_value=[profile]):
        yield store, videos, tmp_path


def _crash_before_db(store):
    real = store._record_change

    def boom(*args, **kwargs):
        raise Crash()
    return mock.patch.object(store, '_record_change', side_effect=boom), real


def _crash_after_db(store):
    return mock.patch.object(store.journal, 'mark_db_committed', side_effect=Crash())


def _overrides(tmp_path):
    path = tmp_path / 'overrides.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else None


def _png():
    buffer = io.BytesIO()
    Image.new('RGB', (20, 20), (200, 10, 10)).save(buffer, 'PNG')
    return 'data:image/png;base64,' + base64.b64encode(buffer.getvalue()).decode()


def _changes(store, resource):
    with store._db() as db:
        return db.execute('SELECT COUNT(*) FROM operator_changes WHERE resource=?', (resource,)).fetchone()[0]


# ---------------------------------------------------------------- profiles

def _patch_profile(store, name='Moi'):
    return store.patch('profiles', 'P1', {'changes': {'ten_san_pham': name}, 'reason': 't'})


def test_profile_patch_crash_before_db_rolls_back_override(ws):
    store, _, tmp = ws
    _patch_profile(store, 'Cu')
    old = _overrides(tmp)
    ctx, _ = _crash_before_db(store)
    with ctx, pytest.raises(Crash):
        _patch_profile(store, 'Moi')
    assert _overrides(tmp)['overrides']['P1']['ten_san_pham'] == 'Moi'
    fresh = WorkspaceStore(store.engine, tmp)
    assert _overrides(tmp) == old
    assert fresh.journal.pending() == []


def test_profile_patch_crash_before_db_first_write_removes_created_file(ws):
    store, _, tmp = ws
    assert _overrides(tmp) is None
    ctx, _ = _crash_before_db(store)
    with ctx, pytest.raises(Crash):
        _patch_profile(store)
    assert _overrides(tmp) is not None
    WorkspaceStore(store.engine, tmp)
    assert _overrides(tmp) is None


def test_profile_patch_crash_after_db_keeps_override_and_is_idempotent(ws):
    store, _, tmp = ws
    with _crash_after_db(store), pytest.raises(Crash):
        _patch_profile(store, 'Moi')
    fresh = WorkspaceStore(store.engine, tmp)
    assert _overrides(tmp)['overrides']['P1']['ten_san_pham'] == 'Moi'
    assert _changes(fresh, 'profiles') == 1
    assert fresh.journal.pending() == [] and fresh.recover_journal() == []


def test_profile_hide_on_delete_crash_both_sides(ws):
    store, _, tmp = ws
    ctx, _ = _crash_before_db(store)
    with ctx, pytest.raises(Crash):
        store.delete('profiles', 'P1', {})
    WorkspaceStore(store.engine, tmp)
    assert _overrides(tmp) is None            # hidden flag rolled back
    with store._db() as db:
        assert db.execute('SELECT COUNT(*) FROM trash_items').fetchone()[0] == 0
    with _crash_after_db(store), pytest.raises(Crash):
        store.delete('profiles', 'P1', {})
    WorkspaceStore(store.engine, tmp)
    assert _overrides(tmp)['overrides']['P1']['hidden'] is True
    with store._db() as db:
        assert db.execute('SELECT COUNT(*) FROM trash_items').fetchone()[0] == 1


# ------------------------------------------------------------ product image

def test_product_image_crash_before_db_restores_previous_image(ws):
    store, _, tmp = ws
    store.save_product_image('p1', {'data_url': _png()})
    image = tmp / 'data' / 'product_images' / 'p1.webp'
    old = image.read_bytes()
    with store._db() as db:
        old_row = tuple(db.execute('SELECT * FROM product_images').fetchone())
    other = io.BytesIO()
    Image.new('RGB', (30, 30), (0, 200, 0)).save(other, 'PNG')
    new_url = 'data:image/png;base64,' + base64.b64encode(other.getvalue()).decode()
    ctx, _ = _crash_before_db(store)
    with ctx, pytest.raises(Crash):
        store.save_product_image('p1', {'data_url': new_url})
    assert image.read_bytes() != old
    fresh = WorkspaceStore(store.engine, tmp)
    assert image.read_bytes() == old
    with fresh._db() as db:
        assert tuple(db.execute('SELECT * FROM product_images').fetchone()) == old_row
    assert fresh.journal.pending() == [] and not list((tmp / 'data' / 'product_images').glob('*.journal.*'))


def test_product_image_first_save_crash_before_db_removes_orphan_file(ws):
    store, _, tmp = ws
    ctx, _ = _crash_before_db(store)
    with ctx, pytest.raises(Crash):
        store.save_product_image('p1', {'data_url': _png()})
    image = tmp / 'data' / 'product_images' / 'p1.webp'
    assert image.exists()
    WorkspaceStore(store.engine, tmp)
    assert not image.exists()


def test_product_image_crash_after_db_commit_keeps_new_image(ws):
    store, _, tmp = ws
    with _crash_after_db(store), pytest.raises(Crash):
        store.save_product_image('p1', {'data_url': _png()})
    fresh = WorkspaceStore(store.engine, tmp)
    image = tmp / 'data' / 'product_images' / 'p1.webp'
    assert image.is_file()
    with fresh._db() as db:
        assert db.execute('SELECT COUNT(*) FROM product_images').fetchone()[0] == 1
    assert fresh.recover_journal() == [] and not list(image.parent.glob('*.journal.*'))


def test_product_image_delete_crash_before_and_after_db(ws):
    store, _, tmp = ws
    store.save_product_image('p1', {'data_url': _png()})
    image = tmp / 'data' / 'product_images' / 'p1.webp'
    data = image.read_bytes()
    ctx, _ = _crash_before_db(store)
    with ctx, pytest.raises(Crash):
        store.delete_product_image('p1')
    assert not image.exists()
    fresh = WorkspaceStore(store.engine, tmp)
    assert image.read_bytes() == data         # moved back, DB row still there
    with fresh._db() as db:
        assert db.execute('SELECT COUNT(*) FROM product_images').fetchone()[0] == 1
    with _crash_after_db(fresh), pytest.raises(Crash):
        fresh.delete_product_image('p1')
    again = WorkspaceStore(store.engine, tmp)
    assert not image.exists()
    archived = list((tmp / 'data' / 'trash' / 'product_images').iterdir())
    assert len(archived) == 1 and archived[0].read_bytes() == data     # media kept in trash
    with again._db() as db:
        assert db.execute('SELECT COUNT(*) FROM product_images').fetchone()[0] == 0


# --------------------------------------------------- video copy (merge) + trash

def test_merge_copy_crash_before_db_removes_copied_file(ws):
    store, videos, tmp = ws
    real = store._record_change

    def crash_on_merge(*args, **kwargs):
        if kwargs.get('action') == 'merge':
            raise Crash()
        return real(*args, **kwargs)
    with mock.patch.object(store, '_record_change', side_effect=crash_on_merge), pytest.raises(Crash):
        store.resolve_video_duplicate('keep', {'other_id': 'other', 'action': 'merge'})
    assert (videos / 'keep' / 'transcript.json').exists()
    WorkspaceStore(store.engine, tmp)
    assert not (videos / 'keep' / 'transcript.json').exists()
    # the original still lives in the trash archive
    archived = list((tmp / 'data' / 'trash' / 'videos').iterdir())
    assert len(archived) == 1 and (archived[0] / 'transcript.json').exists()


def test_merge_copy_crash_after_db_keeps_copied_file(ws):
    store, videos, tmp = ws
    real = store.journal.mark_db_committed
    calls = []

    def crash_on_copy(op_id):
        real(op_id)
        pending = [op for op in store.journal.pending() if op['op_id'] == op_id]
        if pending and pending[0]['kind'] == 'video_copy':
            calls.append(op_id)
            raise Crash()
    with mock.patch.object(store.journal, 'mark_db_committed', side_effect=crash_on_copy), pytest.raises(Crash):
        store.resolve_video_duplicate('keep', {'other_id': 'other', 'action': 'merge'})
    assert calls
    fresh = WorkspaceStore(store.engine, tmp)
    assert (videos / 'keep' / 'transcript.json').exists()
    assert fresh.journal.pending() == []


def _trash_video(store):
    return store.delete('videos', 'other', {})


def test_video_trash_crash_before_db_moves_directory_back(ws):
    store, videos, tmp = ws
    ctx, _ = _crash_before_db(store)
    with ctx, pytest.raises(Crash):
        _trash_video(store)
    assert not (videos / 'other').exists()
    fresh = WorkspaceStore(store.engine, tmp)
    assert (videos / 'other' / 'media.bin').read_bytes() == b'MEDIA-other'
    assert list((tmp / 'data' / 'trash' / 'videos').iterdir()) == []
    assert fresh.journal.pending() == [] and fresh.recover_journal() == []
    with fresh._db() as db:
        assert db.execute('SELECT COUNT(*) FROM trash_items').fetchone()[0] == 0


def test_video_trash_crash_after_db_keeps_archive(ws):
    store, videos, tmp = ws
    with _crash_after_db(store), pytest.raises(Crash):
        _trash_video(store)
    fresh = WorkspaceStore(store.engine, tmp)
    assert not (videos / 'other').exists()
    (archived,) = list((tmp / 'data' / 'trash' / 'videos').iterdir())
    assert (archived / 'media.bin').read_bytes() == b'MEDIA-other'
    with fresh._db() as db:
        assert db.execute('SELECT archived_path FROM trash_items').fetchone()[0] == str(archived)
    assert fresh.recover_journal() == []


def test_video_trash_db_error_moves_directory_back(ws):
    store, videos, tmp = ws
    with mock.patch.object(store, '_delete_commit', side_effect=ValueError('already_in_trash')):
        with pytest.raises(ValueError, match='already_in_trash'):
            _trash_video(store)
    assert (videos / 'other' / 'media.bin').read_bytes() == b'MEDIA-other'    # not stranded in trash
    assert store.journal.pending() == []


def test_video_restore_crash_before_db_moves_back_to_archive(ws):
    store, videos, tmp = ws
    _trash_video(store)
    (archived,) = list((tmp / 'data' / 'trash' / 'videos').iterdir())
    ctx, _ = _crash_before_db(store)
    with ctx, pytest.raises(Crash):
        store.restore('videos', 'other')
    assert (videos / 'other').exists() and not archived.exists()
    fresh = WorkspaceStore(store.engine, tmp)
    assert archived.is_dir() and not (videos / 'other').exists()
    with fresh._db() as db:
        assert db.execute('SELECT restored_at FROM trash_items').fetchone()[0] is None
    assert fresh.restore('videos', 'other')['restored'] is True       # retry works
    assert (videos / 'other' / 'media.bin').read_bytes() == b'MEDIA-other'


def test_video_restore_crash_after_db_keeps_restored_directory(ws):
    store, videos, tmp = ws
    _trash_video(store)
    with _crash_after_db(store), pytest.raises(Crash):
        store.restore('videos', 'other')
    fresh = WorkspaceStore(store.engine, tmp)
    assert (videos / 'other' / 'media.bin').read_bytes() == b'MEDIA-other'
    with fresh._db() as db:
        assert db.execute('SELECT restored_at FROM trash_items').fetchone()[0] is not None
    assert fresh.recover_journal() == []


def test_roll_forward_completes_unfinished_move(ws):
    """DB committed but the rename never happened (journal intent only): recovery finishes it."""
    store, videos, tmp = ws
    src, dst = tmp / 'a_dir', tmp / 'b_dir'
    src.mkdir()
    (src / 'f.bin').write_bytes(b'x')
    op = store.journal.begin_moves('t', [(src, dst)], 'changes:videos:zzz', 0)
    store.journal.mark_db_committed(op)
    assert [a for _, a in store.recover_journal()] == ['rolled_forward']
    assert (dst / 'f.bin').exists() and not src.exists()
    assert store.recover_journal() == []


def test_ambiguous_move_keeps_both_and_never_deletes(ws):
    store, videos, tmp = ws
    src, dst = tmp / 'a_dir', tmp / 'b_dir'
    for folder in (src, dst):
        folder.mkdir()
        (folder / 'f.bin').write_bytes(folder.name.encode())
    store.journal.begin_moves('t', [(src, dst)], 'changes:videos:zzz', 0)
    assert [a for _, a in store.recover_journal()] == ['kept_both']
    assert (src / 'f.bin').read_bytes() == b'a_dir' and (dst / 'f.bin').read_bytes() == b'b_dir'
    assert store.journal.pending() == [] and store.recover_journal() == []


def test_double_recovery_is_idempotent_for_every_kind(ws):
    store, videos, tmp = ws
    ctx, _ = _crash_before_db(store)
    with ctx, pytest.raises(Crash):
        _trash_video(store)
    first = WorkspaceStore(store.engine, tmp)
    second = WorkspaceStore(store.engine, tmp)
    assert second.recover_journal() == []
    assert (videos / 'other' / 'media.bin').read_bytes() == b'MEDIA-other'
