import unittest

from domain import record_fields_changed


class _Record:
    favour = 20
    joy = 30
    trust = 40


class TestRecordFieldsChanged(unittest.TestCase):
    def test_same_values_do_not_create_edit_backup(self):
        self.assertFalse(record_fields_changed(
            _Record(),
            favour=20,
            emotions_absolute={"joy": 30, "trust": 40},
        ))

    def test_changed_favour_or_emotion_requires_backup(self):
        self.assertTrue(record_fields_changed(_Record(), favour=21))
        self.assertTrue(record_fields_changed(
            _Record(), emotions_absolute={"joy": 31},
        ))

    def test_new_record_has_no_existing_snapshot(self):
        self.assertTrue(record_fields_changed(None, favour=20))


if __name__ == "__main__":
    unittest.main()
