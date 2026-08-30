"""emotion_favour utils.py 的 cleanup_old_files（过期文件清理）测试。

utils.py 顶部 import 了 astrbot 等本地未装的库，
按 plugin_local_testing 记忆，把目标顶层函数从源码里以字符串提取出来 exec 到
独立命名空间，绕过模块顶部 import。

运行：
    python3 tests/test_utils.py
    python3 -m unittest tests.test_utils -v
"""
import os
import re
import tempfile
import time
import unittest
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
UTILS_PY = os.path.join(HERE, "..", "utils.py")


def _extract_top_fn(src: str, fn_name: str) -> str:
    lines = src.splitlines(keepends=True)
    start = None
    for i, line in enumerate(lines):
        if re.match(rf"^def {fn_name}\(", line):
            start = i
            break
    if start is None:
        raise ValueError(f"未找到顶层函数 {fn_name}")
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if re.match(r"^(def |class )", lines[j]):
            end = j
            break
    return "".join(lines[start:end])


def _load():
    prefix = "from __future__ import annotations\n\n"
    with open(UTILS_PY, encoding="utf-8") as f:
        ns = {"time": time, "Path": Path}
        exec(prefix + _extract_top_fn(f.read(), "cleanup_old_files"), ns)
        return ns["cleanup_old_files"]


cleanup_old_files = _load()


def _touch(path: Path, age_seconds: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x")
    old = time.time() - age_seconds
    os.utime(path, (old, old))


class TestCleanupOldFiles(unittest.TestCase):
    def test_removes_only_expired_matching_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            _touch(directory / "old.png", age_seconds=10 * 86400)
            _touch(directory / "fresh.png", age_seconds=60)
            _touch(directory / "old.txt", age_seconds=10 * 86400)
            removed = cleanup_old_files(
                directory, suffix=".png", max_age_days=7,
                now=time.time(),
            )
            self.assertEqual(removed, 1)
            self.assertFalse((directory / "old.png").exists())
            self.assertTrue((directory / "fresh.png").exists())
            self.assertTrue((directory / "old.txt").exists())

    def test_missing_directory_returns_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                cleanup_old_files(
                    Path(tmp) / "nonexistent", suffix=".png", max_age_days=7
                ),
                0,
            )

    def test_broken_file_is_skipped(self):
        # stat() 失败的场景较难构造，这里验证子目录（非文件）被跳过
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "sub.png").mkdir()
            removed = cleanup_old_files(
                directory, suffix=".png", max_age_days=0,
                now=time.time(),
            )
            self.assertEqual(removed, 0)

    def test_zero_age_removes_everything(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            _touch(directory / "a.png", age_seconds=3600)
            removed = cleanup_old_files(
                directory, suffix=".png", max_age_days=0,
                now=time.time(),
            )
            self.assertEqual(removed, 1)
            self.assertFalse((directory / "a.png").exists())


if __name__ == "__main__":
    unittest.main()
