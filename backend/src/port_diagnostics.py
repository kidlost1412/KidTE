"""Chẩn đoán cổng Windows chỉ-đọc; không tác động tiến trình đang giữ cổng."""
from __future__ import annotations

import csv
import io
import subprocess


def listening_pids(netstat_output: str, host: str, port: int) -> list[int]:
    wanted = f"{host}:{port}".casefold()
    found = []
    for line in str(netstat_output or "").splitlines():
        columns = line.split()
        if len(columns) < 5 or columns[3].casefold() != "listening":
            continue
        if columns[1].casefold() != wanted:
            continue
        try:
            pid = int(columns[4])
        except ValueError:
            continue
        if pid not in found:
            found.append(pid)
    return found


def process_name(pid: int) -> str:
    """Tên process từ tasklist; lỗi/không có quyền chỉ trả trạng thái rõ."""
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=3, check=False,
        )
        rows = list(csv.reader(io.StringIO(result.stdout)))
    except (OSError, subprocess.SubprocessError):
        return "không xác định"
    if not rows or not rows[0] or rows[0][0].startswith("INFO:"):
        return "không xác định"
    return rows[0][0] or "không xác định"


def owners_for_port(host: str, port: int, *, runner=None) -> list[dict]:
    runner = runner or subprocess.run
    try:
        result = runner(
            ["netstat", "-ano", "-p", "TCP"], capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=3, check=False,
        )
        output = result.stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [{"pid": pid, "process": process_name(pid)}
            for pid in listening_pids(output, host, port)]
