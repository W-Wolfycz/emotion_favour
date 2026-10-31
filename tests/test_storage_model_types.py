"""storage.py 表格模型的时间字段必须声明成 NaiveDatetime。

AstrBot 4.29.3 自带的 SQLModel 0.0.48 把裸 `datetime` 注解映射成 `UTCDateTime`：
**写入要求带时区**（naive 直接抛 `Datetime values must have timezone information`），
**读出自动补 UTC 时区**（再和 naive 的 `utc_now()` 相减就抛 offset-naive/aware）。
本插件全库统一存 naive UTC，一旦有人把字段写回 `datetime`，后台结算会在运行期
整轮失败——日志里只有一条 ERRO，前台完全看不到。

运行：
    python3 -m unittest tests.test_storage_model_types -v
"""
import ast
import unittest
from pathlib import Path

STORAGE = Path(__file__).resolve().parents[1] / "storage.py"

# 只要带时间字段的表模型都列进来；新增表模型时同步加名字
TIME_MODELS = {"FavourRecord", "PersonaSummaryRecord", "PersonaTierScriptRecord"}


def _datetime_fields():
    tree = ast.parse(STORAGE.read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name not in TIME_MODELS:
            continue
        for stmt in node.body:
            if not isinstance(stmt, ast.AnnAssign) or stmt.annotation is None:
                continue
            annotation = ast.unparse(stmt.annotation)
            if "datetime" in annotation.lower():
                found.append((node.name, ast.unparse(stmt.target), annotation))
    return found


class TimeColumnAnnotationTest(unittest.TestCase):
    def test_datetime_columns_are_naive_datetime(self):
        """守「写入整轮失败」：SQLModel 新版把裸 datetime 当 UTCDateTime，
        naive 值写不进去、读出来又带时区——两种症状都只在运行期暴露。"""
        fields = _datetime_fields()
        self.assertTrue(fields, "没扫到任何时间字段，模型名或文件路径已变")
        offenders = [
            f"{model}.{name}: {annotation}"
            for model, name, annotation in fields
            if "NaiveDatetime" not in annotation
        ]
        self.assertEqual(offenders, [], "时间字段必须是 NaiveDatetime")


if __name__ == "__main__":
    unittest.main(verbosity=2)
