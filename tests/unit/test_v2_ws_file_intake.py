"""Phase 10 — WS non-image file intake (ws_file_intake).

纯后端解码逻辑 → 单测即可（无前端面）。锁：
  1. data: URL 解码 + 落盘 + 文件名保留/消毒
  2. 路径穿越文件名被拍平成 basename（安全）
  3. kind 分类（text/audio/video/other）驱动正确的工具提示
  4. 超限/坏块跳过不抛
  5. build_files_note 生成 agent 可读的路径注入块
"""
from __future__ import annotations

import base64

from xmclaw.daemon.ws_file_intake import (
    MAX_FILE_BYTES,
    build_files_note,
    save_user_frame_files,
)


def _data_url(mime: str, payload: bytes) -> str:
    return f"data:{mime};base64,{base64.b64encode(payload).decode()}"


def test_saves_text_file_preserving_name(tmp_path) -> None:
    files = save_user_frame_files(
        [{"name": "report.md", "mime": "text/markdown",
          "data_url": _data_url("text/markdown", b"# hello")}],
        tmp_path,
    )
    assert len(files) == 1
    f = files[0]
    assert f.name == "report.md"
    assert f.kind == "text"
    assert (tmp_path / "report.md").read_bytes() == b"# hello"


def test_path_traversal_name_flattened(tmp_path) -> None:
    files = save_user_frame_files(
        [{"name": "../../etc/passwd", "mime": "text/plain",
          "data_url": _data_url("text/plain", b"x")}],
        tmp_path,
    )
    assert len(files) == 1
    # 落盘必须在 uploads_dir 内，basename 化。
    saved = files[0].path
    assert str(tmp_path) in saved
    assert ".." not in files[0].name


def test_kind_classification(tmp_path) -> None:
    frame = [
        {"name": "a.mp3", "mime": "audio/mpeg", "data_url": _data_url("audio/mpeg", b"a")},
        {"name": "b.mp4", "mime": "video/mp4", "data_url": _data_url("video/mp4", b"b")},
        {"name": "c.bin", "mime": "application/octet-stream",
         "data_url": _data_url("application/octet-stream", b"c")},
    ]
    files = save_user_frame_files(frame, tmp_path)
    kinds = {f.name: f.kind for f in files}
    assert kinds["a.mp3"] == "audio"
    assert kinds["b.mp4"] == "video"
    assert kinds["c.bin"] == "other"


def test_oversized_and_malformed_skipped(tmp_path) -> None:
    huge = b"x" * (MAX_FILE_BYTES + 10)
    frame = [
        {"name": "huge.bin", "mime": "application/octet-stream",
         "data_url": _data_url("application/octet-stream", huge)},
        {"name": "bad.txt", "mime": "text/plain", "data_url": "not-a-data-url"},
        "not-a-dict",
        {"name": "ok.txt", "mime": "text/plain", "data_url": _data_url("text/plain", b"ok")},
    ]
    files = save_user_frame_files(frame, tmp_path)
    # 只有 ok.txt 活下来，过程不抛。
    assert [f.name for f in files] == ["ok.txt"]


def test_missing_extension_inferred_from_mime(tmp_path) -> None:
    files = save_user_frame_files(
        [{"name": "clip", "mime": "audio/wav", "data_url": _data_url("audio/wav", b"w")}],
        tmp_path,
    )
    assert files[0].name.endswith(".wav")


def test_non_list_returns_empty(tmp_path) -> None:
    assert save_user_frame_files(None, tmp_path) == []
    assert save_user_frame_files("nope", tmp_path) == []


def test_build_files_note_mentions_path_and_tool() -> None:
    # 直接构造 SavedFile 以独立测 note 渲染。
    from xmclaw.daemon.ws_file_intake import SavedFile

    note = build_files_note([
        SavedFile(path="/u/a.txt", name="a.txt", mime="text/plain", bytes_size=2048, kind="text"),
        SavedFile(path="/u/v.mp4", name="v.mp4", mime="video/mp4", bytes_size=4096, kind="video"),
    ])
    assert "/u/a.txt" in note
    assert "file_read" in note  # 文本 → file_read 提示
    assert "/u/v.mp4" in note
    assert "<user-uploaded-files>" in note and "</user-uploaded-files>" in note


def test_build_files_note_empty_for_no_files() -> None:
    assert build_files_note([]) == ""
