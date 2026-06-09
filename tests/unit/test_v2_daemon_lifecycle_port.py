"""daemon 生命周期:按端口属主 stop/start —— 修"重启没杀旧进程"。

根因:stop/start 只信 daemon.pid;pid 文件失准时 stop 杀错进程、真正占端口的旧
daemon 永活,start 又因 _http_healthy 只看"端口有应答"误判成功 → 两 daemon 并存。
修复:按「谁真正 LISTEN 在端口」来 杀/判。
"""
from __future__ import annotations

import socket

import pytest

from xmclaw.daemon import lifecycle as lc


def test_port_listener_pid_finds_real_listener():
    import os
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen()
    port = s.getsockname()[1]
    try:
        assert lc._port_listener_pid(port) == os.getpid()
    finally:
        s.close()


def test_port_listener_pid_none_for_free_port():
    # 拿一个临时端口再关掉 → 没人 listen
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    assert lc._port_listener_pid(port) is None


def test_start_refuses_when_port_has_healthy_daemon(monkeypatch):
    monkeypatch.setattr(lc, "_http_healthy", lambda h, p, **k: True)
    monkeypatch.setattr(lc, "_port_listener_pid", lambda p: 4242)
    with pytest.raises(RuntimeError, match="already running"):
        lc.start_daemon(host="127.0.0.1", port=8766, config="x", wait_seconds=0.1)


def test_start_reclaims_orphan_then_spawns(monkeypatch, tmp_path):
    # 端口被占但不健康 = 僵尸 → start 必须先回收再起。
    killed: list[int] = []
    monkeypatch.setattr(lc, "_http_healthy", lambda h, p, **k: False)
    monkeypatch.setattr(lc, "read_status",
                        lambda: lc.DaemonStatus("dead", None, None, None, False))
    monkeypatch.setattr(lc, "_clear_files", lambda: None)
    monkeypatch.setattr(lc, "_write_meta", lambda h, p: None)
    monkeypatch.setattr(lc, "default_pid_path", lambda: tmp_path / "d.pid")
    monkeypatch.setattr(lc, "default_log_path", lambda: tmp_path / "d.log")

    # _reclaim_port 内部:第一次有属主(僵尸),杀完返回它的 pid
    seq = iter([9999, None, None])
    monkeypatch.setattr(lc, "_port_listener_pid", lambda p: next(seq, None))
    monkeypatch.setattr(lc, "_force_kill", lambda pid: killed.append(pid))

    # 不真起进程:让 Popen 返回个假对象,health 立刻 False → 超时退出即可
    class _FakeProc:
        pid = 12345
    monkeypatch.setattr(lc.subprocess, "Popen", lambda *a, **k: _FakeProc())
    monkeypatch.setattr(lc, "_process_alive", lambda pid: False)  # 立即"退出"→快速返回

    with pytest.raises(RuntimeError):  # daemon exited before healthy(预期)
        lc.start_daemon(host="127.0.0.1", port=8766, config="x", wait_seconds=0.1)
    assert 9999 in killed  # 僵尸被回收了


def test_stop_reclaims_port_owner(monkeypatch):
    killed: list[int] = []
    monkeypatch.setattr(lc, "read_status",
                        lambda: lc.DaemonStatus("running", 111, "127.0.0.1", 8766, True))
    # daemon.pid=111 杀完后端口仍被 222(真属主)占着 → 必须再回收
    monkeypatch.setattr(lc, "_process_alive", lambda pid: False)
    monkeypatch.setattr(lc, "_clear_files", lambda: None)
    monkeypatch.setattr(lc, "_port_listener_pid", lambda p: 222)
    monkeypatch.setattr(lc, "_force_kill", lambda pid: killed.append(pid))
    monkeypatch.setattr(lc.subprocess, "run", lambda *a, **k: None)
    lc.stop_daemon(grace_seconds=0.1)
    assert 222 in killed  # 真正占端口的 orphan 被 stop 清掉
