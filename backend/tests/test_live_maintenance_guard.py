import time
import pytest
from server import ReplyEngine


@pytest.mark.parametrize('platform', ['shopee', 'tiktok'])
def test_restore_is_rejected_before_touching_backup_during_either_live(platform):
    engine = ReplyEngine.__new__(ReplyEngine)
    engine.extension_heartbeat = None
    engine.platform_heartbeats = {platform: {'live_running': True, 'received_at': time.time()}}
    with pytest.raises(ValueError, match='maintenance_required'):
        engine.restore_backup({'backup_id': 'must-not-be-opened'})


@pytest.mark.parametrize('timestamp', [float('nan'), float('inf'), 'invalid', time.time() - 100])
def test_invalid_or_stale_heartbeat_does_not_claim_live(timestamp):
    engine = ReplyEngine.__new__(ReplyEngine)
    engine.extension_heartbeat = None
    engine.platform_heartbeats = {'shopee': {'live_running': True, 'received_at': timestamp}}
    assert engine._is_live_running() is False
