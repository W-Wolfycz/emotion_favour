"""管理台编辑保存的变更检测：值没变不备份、值变了必须先备份。

`record_fields_changed` 的结果决定 `set_record_fields` 走「原样返回」还是
「备份后写入」。判错任一方向都不报错：多备份只在备份列表里悄悄多出文件，
少备份要等到用备份恢复时才发现保护缺了一格。两个方向合并成一条用例。

运行：
    python3 -m unittest tests.test_admin_backup_logic -v
"""
import unittest

from domain import record_fields_changed


class _Record:
    favour = 20
    joy = 30
    trust = 40


class TestRecordFieldsChanged(unittest.TestCase):
    def test_change_detection_decides_backup(self):
        """守两种静默失效：无变化时多做备份（备份目录膨胀）、有变化时漏备份
        （编辑前保护消失）。好感度与情感是两条独立判定分支，都要盯。"""
        record = _Record()
        self.assertFalse(record_fields_changed(
            record,
            favour=20,
            emotions_absolute={"joy": 30, "trust": 40},
        ))
        self.assertTrue(record_fields_changed(record, favour=21))
        self.assertTrue(record_fields_changed(record, emotions_absolute={"joy": 31}))

if __name__ == "__main__":
    unittest.main()
