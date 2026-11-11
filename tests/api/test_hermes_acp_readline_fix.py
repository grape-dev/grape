# -*- coding: utf-8 -*-
"""回归测试：Windows 下 hermes ACP reader 用 read(4096)+行缓冲替代 readline，
验证能正确处理「尾行不带换行」的流式输出（readline 会阻塞挂死）。"""
import subprocess
import time


def test_read_buf_handles_tail_line_without_newline():
    """read(4096)+行缓冲：喂入含『无换行尾行』的数据，完整行即时切出，尾行残留待补全。"""
    _read_buf = b""
    handled = []

    def feed(chunk: bytes):
        nonlocal _read_buf
        _read_buf += chunk
        while b"\n" in _read_buf:
            line, _read_buf = _read_buf.split(b"\n", 1)
            s = line.decode("utf-8", errors="replace").strip()
            if s:
                handled.append(s)

    # 模拟 hermes 一次性 flush 多行 JSON-RPC，尾行未带换行（readline 在此会阻塞等 \n）
    feed(b'{"jsonrpc":"2.0","result":"line1"}\n{"jsonrpc":"2.0","result":"line2"}\n{"jsonrpc":"2.0","result":"line3-tail')
    assert len(handled) == 2, f"应切出 2 完整行，实际 {len(handled)}"
    assert _read_buf == b'{"jsonrpc":"2.0","result":"line3-tail', f"尾行残留不符：{_read_buf}"

    # 补全尾行换行，尾行应能正确切出
    feed(b'...finish"}\n')
    assert len(handled) == 3, f"补全后应 3 行，实际 {len(handled)}"
    assert handled[2].startswith('{"jsonrpc":"2.0","result":"line3-tail')


def test_fileio_read_returns_partial_without_blocking_for_full():
    """bufsize=0 时 stdout 是 FileIO（无 read1）；read(4096) 读已 flush 字节、不阻塞等满。"""
    p = subprocess.Popen(
        ["python", "-c",
         "import sys,time; sys.stdout.write('A'*10); sys.stdout.flush(); time.sleep(1)"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
    )
    try:
        assert type(p.stdout).__name__ == "FileIO", f"期望 FileIO，实际 {type(p.stdout).__name__}"
        assert not hasattr(p.stdout, "read1"), "bufsize=0 时 FileIO 无 read1（read1 方案不可行）"
        t0 = time.time()
        chunk = p.stdout.read(4096)
        dt = time.time() - t0
        assert len(chunk) == 10, f"应读到 10 字节部分数据，实际 {len(chunk)}"
        assert dt < 0.5, f"read 不应阻塞等满 4096，耗时 {dt:.2f}s"
    finally:
        p.kill()
        p.wait()