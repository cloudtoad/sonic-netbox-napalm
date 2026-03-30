"""Tests for config checkpoint versioning."""

import json
import os
import tarfile
import tempfile
import time
import unittest

from config.config_versioning import (
    create_checkpoint,
    extract_checkpoint,
    format_size,
    list_checkpoints,
)


class TestCreateCheckpoint(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.config_db = os.path.join(self.tmpdir, "config_db.json")
        self.frr_dir = os.path.join(self.tmpdir, "frr")
        self.checkpoint_dir = os.path.join(self.tmpdir, "checkpoints")
        os.makedirs(self.frr_dir)

        # Write test config files
        with open(self.config_db, "w") as f:
            json.dump({"PORT": {"Ethernet0": {"speed": "25000"}}}, f)
        with open(os.path.join(self.frr_dir, "bgpd.conf"), "w") as f:
            f.write("router bgp 65001\n")
        with open(os.path.join(self.frr_dir, "zebra.conf"), "w") as f:
            f.write("hostname test\n")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir)

    def test_creates_archive(self):
        path = create_checkpoint(
            trigger="test",
            config_db_file=self.config_db,
            frr_dir=self.frr_dir,
            checkpoint_dir=self.checkpoint_dir,
        )
        self.assertIsNotNone(path)
        self.assertTrue(path.endswith("checkpoint.001.tar.gz"))
        self.assertTrue(os.path.exists(path))

    def test_archive_contains_all_files(self):
        path = create_checkpoint(
            trigger="test",
            config_db_file=self.config_db,
            frr_dir=self.frr_dir,
            checkpoint_dir=self.checkpoint_dir,
        )
        with tarfile.open(path, "r:gz") as tar:
            names = tar.getnames()
        self.assertIn("config_db.json", names)
        self.assertIn("frr/bgpd.conf", names)
        self.assertIn("frr/zebra.conf", names)
        self.assertIn("metadata.json", names)

    def test_metadata_content(self):
        path = create_checkpoint(
            trigger="config save",
            config_db_file=self.config_db,
            frr_dir=self.frr_dir,
            checkpoint_dir=self.checkpoint_dir,
        )
        with tarfile.open(path, "r:gz") as tar:
            meta = json.loads(tar.extractfile("metadata.json").read())
        self.assertEqual(meta["trigger"], "config save")
        self.assertIn("timestamp", meta)
        self.assertIn("sonic_version", meta)

    def test_rotation(self):
        for i in range(3):
            create_checkpoint(
                trigger=f"save-{i}",
                config_db_file=self.config_db,
                frr_dir=self.frr_dir,
                checkpoint_dir=self.checkpoint_dir,
            )

        # Should have .001, .002, .003
        for v in (1, 2, 3):
            self.assertTrue(
                os.path.exists(
                    os.path.join(self.checkpoint_dir, f"checkpoint.{v:03d}.tar.gz")
                )
            )

        # .001 should be the most recent (save-2)
        with tarfile.open(
            os.path.join(self.checkpoint_dir, "checkpoint.001.tar.gz"), "r:gz"
        ) as tar:
            meta = json.loads(tar.extractfile("metadata.json").read())
        self.assertEqual(meta["trigger"], "save-2")

    def test_max_checkpoints_enforced(self):
        for i in range(5):
            create_checkpoint(
                trigger=f"save-{i}",
                config_db_file=self.config_db,
                frr_dir=self.frr_dir,
                checkpoint_dir=self.checkpoint_dir,
                max_checkpoints=3,
            )

        # Only 3 should exist
        checkpoints = list_checkpoints(self.checkpoint_dir)
        self.assertEqual(len(checkpoints), 3)

    def test_no_config_db(self):
        """Checkpoint works even if config_db.json doesn't exist."""
        os.remove(self.config_db)
        path = create_checkpoint(
            config_db_file=self.config_db,
            frr_dir=self.frr_dir,
            checkpoint_dir=self.checkpoint_dir,
        )
        self.assertIsNotNone(path)
        with tarfile.open(path, "r:gz") as tar:
            names = tar.getnames()
        self.assertNotIn("config_db.json", names)
        self.assertIn("frr/bgpd.conf", names)


class TestExtractCheckpoint(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.config_db = os.path.join(self.tmpdir, "config_db.json")
        self.frr_dir = os.path.join(self.tmpdir, "frr")
        self.checkpoint_dir = os.path.join(self.tmpdir, "checkpoints")
        os.makedirs(self.frr_dir)

        with open(self.config_db, "w") as f:
            json.dump({"PORT": {"Ethernet0": {"speed": "25000"}}}, f)
        with open(os.path.join(self.frr_dir, "bgpd.conf"), "w") as f:
            f.write("router bgp 65001\n")

        create_checkpoint(
            trigger="test",
            config_db_file=self.config_db,
            frr_dir=self.frr_dir,
            checkpoint_dir=self.checkpoint_dir,
        )

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir)

    def test_extracts_files(self):
        extract_dir = extract_checkpoint(version=1, checkpoint_dir=self.checkpoint_dir)
        self.assertIsNotNone(extract_dir)
        self.assertTrue(os.path.exists(os.path.join(extract_dir, "config_db.json")))
        self.assertTrue(os.path.exists(os.path.join(extract_dir, "frr", "bgpd.conf")))

        # Verify content
        with open(os.path.join(extract_dir, "config_db.json")) as f:
            data = json.load(f)
        self.assertIn("PORT", data)

        import shutil
        shutil.rmtree(extract_dir)

    def test_nonexistent_version(self):
        result = extract_checkpoint(version=99, checkpoint_dir=self.checkpoint_dir)
        self.assertIsNone(result)


class TestListCheckpoints(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.config_db = os.path.join(self.tmpdir, "config_db.json")
        self.frr_dir = os.path.join(self.tmpdir, "frr")
        self.checkpoint_dir = os.path.join(self.tmpdir, "checkpoints")
        os.makedirs(self.frr_dir)

        with open(self.config_db, "w") as f:
            json.dump({}, f)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir)

    def test_empty(self):
        self.assertEqual(list_checkpoints(self.checkpoint_dir), [])

    def test_lists_in_order(self):
        for i in range(3):
            create_checkpoint(
                trigger=f"save-{i}",
                config_db_file=self.config_db,
                frr_dir=self.frr_dir,
                checkpoint_dir=self.checkpoint_dir,
            )

        checkpoints = list_checkpoints(self.checkpoint_dir)
        self.assertEqual(len(checkpoints), 3)
        self.assertEqual(checkpoints[0]["version"], 1)
        self.assertEqual(checkpoints[1]["version"], 2)
        self.assertEqual(checkpoints[2]["version"], 3)

    def test_includes_metadata(self):
        create_checkpoint(
            trigger="unit-test",
            config_db_file=self.config_db,
            frr_dir=self.frr_dir,
            checkpoint_dir=self.checkpoint_dir,
        )
        checkpoints = list_checkpoints(self.checkpoint_dir)
        self.assertEqual(checkpoints[0]["trigger"], "unit-test")
        self.assertNotEqual(checkpoints[0]["timestamp"], "")


class TestFormatSize(unittest.TestCase):
    def test_bytes(self):
        self.assertEqual(format_size(500), "500 B")

    def test_kilobytes(self):
        self.assertEqual(format_size(39200), "38.3 KB")

    def test_megabytes(self):
        self.assertEqual(format_size(2 * 1024 * 1024), "2.0 MB")


if __name__ == "__main__":
    unittest.main()
