import os
from obs_source import MediaBindingStore, source_identity
from media_assets import media_key


def test_same_name_replaced_invalidates(tmp_path):
    f = tmp_path / "a.mp4"
    f.write_bytes(b"1234")
    k1, m1 = source_identity(f), media_key(f)
    f.write_bytes(b"5678")  # same size, new mtime
    os.utime(f, ns=(1_000_000_000_000, 1_000_000_000_000))
    assert source_identity(f) != k1 and media_key(f) != m1


def test_library_revision_bump_invalidates(tmp_path):
    f = tmp_path / "a.mp4"
    f.write_bytes(b"1234")
    store = MediaBindingStore(tmp_path / "t.db")
    k1, m1 = source_identity(f, store.library_revision()), media_key(f, store.library_revision())
    store.bump_library_revision()
    assert source_identity(f, store.library_revision()) != k1
    assert media_key(f, store.library_revision()) != m1


def test_manual_binding_persists(tmp_path):
    f = tmp_path / "a.mp4"
    f.write_bytes(b"1")
    MediaBindingStore(tmp_path / "t.db").set_manual_binding(f, "SKU1")
    again = MediaBindingStore(tmp_path / "t.db")
    assert again.manual_binding(f) == "SKU1" and again.library_revision() == 1
