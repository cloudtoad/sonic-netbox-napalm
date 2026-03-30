"""Tests for config save versioning."""

import json
import os
import tempfile
import time
import unittest

from config_versioning import (
    format_size,
    list_config_history,
    rotate_config_file,
)


class TestRotateConfigFile(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.config_path = os.path.join(self.tmpdir, "config_db.json")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir)

    def _write_config(self, path, content="{}"):
        with open(path, "w") as f:
            f.write(content)

    def test_no_existing_file(self):
        """rotate_config_file returns None if file doesn't exist."""
        result = rotate_config_file(self.config_path)
        self.assertIsNone(result)

    def test_first_rotation(self):
        """First save creates .001 backup."""
        self._write_config(self.config_path, '{"version": 1}')
        result = rotate_config_file(self.config_path)

        self.assertEqual(result, f"{self.config_path}.001")
        self.assertTrue(os.path.exists(f"{self.config_path}.001"))
        self.assertFalse(os.path.exists(self.config_path))

        with open(f"{self.config_path}.001") as f:
            data = json.load(f)
        self.assertEqual(data["version"], 1)

    def test_multiple_rotations(self):
        """Successive rotations increment version numbers."""
        # Create initial + two backups
        self._write_config(f"{self.config_path}.002", '{"version": "a"}')
        self._write_config(f"{self.config_path}.001", '{"version": "b"}')
        self._write_config(self.config_path, '{"version": "c"}')

        rotate_config_file(self.config_path)

        # .002 -> .003, .001 -> .002, current -> .001
        self.assertTrue(os.path.exists(f"{self.config_path}.001"))
        self.assertTrue(os.path.exists(f"{self.config_path}.002"))
        self.assertTrue(os.path.exists(f"{self.config_path}.003"))

        with open(f"{self.config_path}.001") as f:
            self.assertEqual(json.load(f)["version"], "c")
        with open(f"{self.config_path}.002") as f:
            self.assertEqual(json.load(f)["version"], "b")
        with open(f"{self.config_path}.003") as f:
            self.assertEqual(json.load(f)["version"], "a")

    def test_max_backups_enforced(self):
        """Oldest backups are removed when max_backups is reached."""
        # Create 3 existing backups
        self._write_config(f"{self.config_path}.001", '{"v": 1}')
        self._write_config(f"{self.config_path}.002", '{"v": 2}')
        self._write_config(f"{self.config_path}.003", '{"v": 3}')
        self._write_config(self.config_path, '{"v": "current"}')

        rotate_config_file(self.config_path, max_backups=3)

        # .003 should have been removed (exceeded limit)
        # .002 -> .003, .001 -> .002, current -> .001
        self.assertTrue(os.path.exists(f"{self.config_path}.001"))
        self.assertTrue(os.path.exists(f"{self.config_path}.002"))
        self.assertTrue(os.path.exists(f"{self.config_path}.003"))
        self.assertFalse(os.path.exists(f"{self.config_path}.004"))

    def test_non_numeric_suffixes_ignored(self):
        """Files with non-numeric suffixes are not affected."""
        self._write_config(self.config_path, '{}')
        self._write_config(f"{self.config_path}.bak", '{}')
        self._write_config(f"{self.config_path}.old", '{}')

        rotate_config_file(self.config_path)

        # .bak and .old should be untouched
        self.assertTrue(os.path.exists(f"{self.config_path}.bak"))
        self.assertTrue(os.path.exists(f"{self.config_path}.old"))
        self.assertTrue(os.path.exists(f"{self.config_path}.001"))


class TestListConfigHistory(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.config_path = os.path.join(self.tmpdir, "config_db.json")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir)

    def test_no_backups(self):
        history = list_config_history(self.config_path)
        self.assertEqual(history, [])

    def test_lists_backups_in_order(self):
        for i in range(1, 4):
            path = f"{self.config_path}.{i:03d}"
            with open(path, "w") as f:
                f.write("{}")
            # Stagger mtimes
            os.utime(path, (time.time() - (3 - i) * 60, time.time() - (3 - i) * 60))

        history = list_config_history(self.config_path)
        self.assertEqual(len(history), 3)
        self.assertEqual(history[0]["version"], 1)
        self.assertEqual(history[1]["version"], 2)
        self.assertEqual(history[2]["version"], 3)


class TestFormatSize(unittest.TestCase):
    def test_bytes(self):
        self.assertEqual(format_size(500), "500 B")

    def test_kilobytes(self):
        self.assertEqual(format_size(39200), "38.3 KB")

    def test_megabytes(self):
        self.assertEqual(format_size(2 * 1024 * 1024), "2.0 MB")


if __name__ == "__main__":
    unittest.main()
