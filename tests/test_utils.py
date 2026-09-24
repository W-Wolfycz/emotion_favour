"""utils.cleanup_old_files（过期文件清理）的行为测试。

只覆盖「改错了一眼看不出」的点：清理不按后缀/年龄过滤会静默删掉不该删的文件，
或让 T2I 缓存无限增长。

运行：
    python3 -m unittest tests.test_utils -v
"""
import importlib.util
import os
import tempfile
import time
import unittest
from pathlib import Path


def _load_shared():
    """按路径加载 tests/_shared.py（pytest 下不能按包名 import）。"""
    path = Path(__file__).resolve().parent / "_shared.py"
    spec = importlib.util.spec_from_file_location("ef_tests_shared", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

_shared = _load_shared()
PLUGIN_DIR = _shared.PLUGIN_DIR
load_top_function = _shared.load_top_function

cleanup_old_files = load_top_function(
    PLUGIN_DIR / "utils.py", "cleanup_old_files", namespace={"time": time, "Path": Path}
)


def _touch(path: Path, age_seconds: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x")
    old = time.time() - age_seconds
    os.utime(path, (old, old))


class TestCleanupOldFiles(unittest.TestCase):
    def test_removes_only_expired_matching_files(self):
        """守两种静默失效：漏了后缀过滤会删掉同目录的非缓存文件（不可恢复），
        漏了年龄过滤/阈值算错则缓存永不回收；max_age_days=0 表示全部过期。"""
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            _touch(directory / "old.png", age_seconds=10 * 86400)
            _touch(directory / "fresh.png", age_seconds=60)
            _touch(directory / "old.txt", age_seconds=10 * 86400)
            removed = cleanup_old_files(
                directory, suffix=".png", max_age_days=7, now=time.time()
            )
            self.assertEqual(removed, 1)
            self.assertFalse((directory / "old.png").exists())
            self.assertTrue((directory / "fresh.png").exists(), "未过期的不该删")
            self.assertTrue((directory / "old.txt").exists(), "后缀不匹配的不该删")

            removed = cleanup_old_files(
                directory, suffix=".png", max_age_days=0, now=time.time()
            )
            self.assertEqual(removed, 1, "max_age_days=0 表示全部过期")
            self.assertFalse((directory / "fresh.png").exists())

if __name__ == "__main__":
    unittest.main()
