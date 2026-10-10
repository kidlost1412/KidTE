import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from port_diagnostics import listening_pids


def test_reads_only_matching_listening_tcp_rows():
    output = """
  TCP    127.0.0.1:8793       0.0.0.0:0              LISTENING       1234
  TCP    127.0.0.1:8793       127.0.0.1:53000        ESTABLISHED     1234
  TCP    127.0.0.1:9999       0.0.0.0:0              LISTENING       9999
  TCP    [::1]:8793           [::]:0                 LISTENING       2222
"""
    assert listening_pids(output, "127.0.0.1", 8793) == [1234]
