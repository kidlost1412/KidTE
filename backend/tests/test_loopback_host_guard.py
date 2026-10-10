"""Hoi quy F05: Host phai khop TUYET DOI, khong dung tien to.

Ban cu: host.startswith(("127.0.0.1", "localhost")). Ke tan cong dang ky
"localhost.evil.com" tro DNS ve 127.0.0.1 (DNS rebinding); trinh duyet gui
Host: localhost.evil.com:8793 va Origin: http://localhost.evil.com:8793.
Dieu kien cu cho qua tien to, con `origin == f"http://{host}"` thi khop
ngay vi ca hai deu la ten mien cua ke tan cong.
"""
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from server import _loopback_host  # noqa: E402


class LoopbackHostTests(unittest.TestCase):
    def test_chap_nhan_host_loopback_that(self):
        for host in ("127.0.0.1", "127.0.0.1:8793", "localhost", "localhost:8793",
                     "LOCALHOST:8793", "[::1]", "[::1]:8793"):
            with self.subTest(host=host):
                self.assertTrue(_loopback_host(host))

    def test_tu_choi_hostname_gia_co_tien_to_hop_le(self):
        for host in ("localhost.evil.com", "localhost.evil.com:8793",
                     "127.0.0.1.attacker.net", "127.0.0.1.attacker.net:8793",
                     "localhost-evil.com", "127.0.0.10", "127.0.0.1x"):
            with self.subTest(host=host):
                self.assertFalse(_loopback_host(host),
                                 f"{host} khong duoc coi la loopback")

    def test_tu_choi_host_rong_hoac_di_dang(self):
        for host in ("", "   ", None, "[::1", "localhost:abc", "example.com"):
            with self.subTest(host=host):
                self.assertFalse(_loopback_host(host))


if __name__ == "__main__":
    unittest.main()
