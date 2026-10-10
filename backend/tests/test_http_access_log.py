"""HTTP replies must not depend on the state of the launcher console."""

import contextlib
import io
import sys
import threading
import urllib.request
from types import SimpleNamespace
import pytest
from pathlib import Path


SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from server import Handler, LoopbackHTTPServer  # noqa: E402


class BlockedConsole(io.TextIOBase):
    def write(self, _text):
        raise OSError("launcher console is unavailable")


@pytest.mark.parametrize('error', [BrokenPipeError, ConnectionAbortedError, ConnectionResetError])
@pytest.mark.parametrize('stage', ['headers', 'body'])
def test_cancelled_json_response_does_not_become_backend_error(error, stage):
    def disconnected(*_args):
        raise error('browser cancelled request')
    handler = SimpleNamespace(send_response=lambda *_: None, cors=lambda: None,
        send_header=lambda *_: None, end_headers=disconnected if stage == 'headers' else lambda: None,
        wfile=SimpleNamespace(write=disconnected if stage == 'body' else lambda *_: None),
        close_connection=False)
    Handler.send_json(handler, 200, {'ok': True, 'receipt': 'already-persisted'})
    assert handler.close_connection is True


def test_app_page_responds_when_stdout_is_unavailable():
    server = LoopbackHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with contextlib.redirect_stdout(BlockedConsole()):
            with urllib.request.urlopen(
                f"http://127.0.0.1:{server.server_port}/app", timeout=3
            ) as response:
                assert response.status == 200
                assert b"Kid TE" in response.read()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
