import re
import socket
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import server as srv  # noqa: E402


def test_listen_backlog_and_threads():
    assert srv.LoopbackHTTPServer.request_queue_size >= 64
    assert srv.LoopbackHTTPServer.daemon_threads is True


def test_operator_page_gets_version_query():
    html = Path(srv.WEB_DIR / "operator.html").read_text(encoding="utf-8")
    out = srv.versioned_operator_page(html, "1.15.1")
    assert 'name="kid-te-version" content="1.15.1"' in out
    assert '/assets/styles/video.css?v=1.15.1"' in out
    assert '/assets/app/bootstrap.js?v=1.15.1"' in out
    assert not re.search(r'(?:href|src)="/assets/[^"?]+"', out)


def test_burst_of_slow_connections_all_served():
    class Slow(BaseHTTPRequestHandler):
        def do_GET(self):
            threading.Event().wait(0.15)
            self.send_response(200); self.send_header("Content-Length", "2"); self.end_headers(); self.wfile.write(b"ok")
        def log_message(self, *a): pass
    httpd = srv.LoopbackHTTPServer(("127.0.0.1", 0), Slow)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    def fetch(_):
        with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
            s.sendall(b"GET / HTTP/1.0\r\n\r\n")
            data = b""
            while chunk := s.recv(4096):
                data += chunk
            return data.endswith(b"ok")
    try:
        with ThreadPoolExecutor(40) as pool:
            assert all(pool.map(fetch, range(40)))
    finally:
        httpd.shutdown(); httpd.server_close()


def test_handler_has_socket_timeout():
    assert 0 < srv.Handler.timeout <= 60


def test_over_capacity_gets_fast_503_and_slots_are_released():
    release = threading.Event()

    class Block(BaseHTTPRequestHandler):
        def do_GET(self):
            release.wait(10)
            self.send_response(200); self.send_header("Content-Length", "2"); self.end_headers(); self.wfile.write(b"ok")
        def log_message(self, *a): pass

    class Small(srv.LoopbackHTTPServer):
        max_concurrent_requests = 2

    httpd = Small(("127.0.0.1", 0), Block)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]

    def fetch(_=None):
        with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
            s.sendall(b"GET / HTTP/1.0\r\n\r\n")
            data = b""
            while chunk := s.recv(4096):
                data += chunk
            return data

    try:
        pool = ThreadPoolExecutor(2)
        busy = [pool.submit(fetch) for _ in range(2)]
        import time
        deadline = time.time() + 5
        while httpd._slots._value and time.time() < deadline:
            time.sleep(0.01)
        start = time.time()
        rejected = fetch()
        assert rejected.startswith(b"HTTP/1.1 503") and b"server_busy" in rejected
        assert time.time() - start < 3
        release.set()
        assert all(f.result(10).endswith(b"ok") for f in busy)
        pool.shutdown()
        deadline = time.time() + 5
        while httpd._slots._value < 2 and time.time() < deadline:
            time.sleep(0.01)
        assert fetch().endswith(b"ok")          # luồng đã trả lại chỗ
    finally:
        release.set(); httpd.shutdown(); httpd.server_close()
