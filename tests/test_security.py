from pathlib import Path
from xmclaw.utils.security import is_path_safe


def test_is_path_safe_within_base():
    base = Path(r"C:\Users\15978\Desktop\XMclaw")
    target = base / "tmp" / "test.txt"
    assert is_path_safe(target, base) is True


def test_is_path_safe_outside_base():
    base = Path(r"C:\Users\15978\Desktop\XMclaw")
    target = Path(r"C:\Windows\System32")
    assert is_path_safe(target, base) is False


def test_is_path_safe_traversal():
    base = Path(r"C:\Users\15978\Desktop\XMclaw")
    target = base / ".." / ".." / "secret.txt"
    assert is_path_safe(target, base) is False
