"""utils.cleanup_old_files（过期文件清理）的行为测试。

只覆盖「改错了一眼看不出」的点：缓存清理若不按后缀/年龄过滤，会静默删掉
不该删的文件或让缓存无限增长；其余分支（缺失目录、子目录）只是防御性守卫，
合并成一条记录行为即可。

运行：
    python3 -m unittest tests.test_utils -v
"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import PLUGIN_DIR, load_top_function  # noqa: E402

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
        """后缀、年龄两道过滤都要生效，且 max_age_days=0 视为全部过期。"""
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
