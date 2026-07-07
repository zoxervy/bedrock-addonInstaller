import io
import json
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import addonInstaller


PACK_UUID = "12345678-1234-1234-1234-123456789abc"
OTHER_UUID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


class AddonInstallerSafetyTests(unittest.TestCase):
    def setUp(self):
        self._old_dry_run = addonInstaller.DRY_RUN
        self.addCleanup(self._restore_dry_run)

    def _restore_dry_run(self):
        addonInstaller.DRY_RUN = self._old_dry_run

    def make_manifest(self, kind="resources", version=None):
        return {
            "format_version": 2,
            "header": {
                "name": "Test Pack",
                "uuid": PACK_UUID,
                "version": version or [1, 0, 0],
            },
            "modules": [
                {
                    "type": kind,
                    "uuid": "87654321-4321-4321-4321-cba987654321",
                    "version": [1, 0, 0],
                }
            ],
        }

    def test_safe_extract_zip_extracts_normal_file(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            archive = root / "pack.mcpack"
            dest = root / "out"
            with zipfile.ZipFile(archive, "w") as z:
                z.writestr("folder/file.txt", "hello")

            with mock.patch.object(addonInstaller, "print_progress"):
                addonInstaller.safe_extract(archive, dest)

            self.assertEqual((dest / "folder" / "file.txt").read_text(), "hello")

    def test_safe_extract_zip_blocks_path_traversal(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            archive = root / "evil.mcpack"
            dest = root / "out"
            with zipfile.ZipFile(archive, "w") as z:
                z.writestr("../evil.txt", "bad")

            with self.assertRaisesRegex(RuntimeError, "Path traversal blocked"):
                addonInstaller.safe_extract(archive, dest)
            self.assertFalse((root / "evil.txt").exists())

    def test_safe_extract_tar_blocks_path_traversal(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            archive = root / "evil.tar.gz"
            dest = root / "out"
            data = b"bad"
            info = tarfile.TarInfo("../evil.txt")
            info.size = len(data)
            with tarfile.open(archive, "w:gz") as tf:
                tf.addfile(info, io.BytesIO(data))

            with self.assertRaisesRegex(RuntimeError, "Path traversal blocked"):
                addonInstaller.safe_extract_tar(archive, dest)
            self.assertFalse((root / "evil.txt").exists())

    def test_safe_extract_tar_blocks_symlink(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            archive = root / "link.tar.gz"
            dest = root / "out"
            info = tarfile.TarInfo("link")
            info.type = tarfile.SYMTYPE
            info.linkname = "target"
            with tarfile.open(archive, "w:gz") as tf:
                tf.addfile(info)

            with self.assertRaisesRegex(RuntimeError, "Unsafe tar member blocked"):
                addonInstaller.safe_extract_tar(archive, dest)

    def test_extract_archive_to_temp_raises_in_dry_run(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            archive = root / "pack.mcpack"
            with zipfile.ZipFile(archive, "w") as z:
                z.writestr("manifest.json", json.dumps(self.make_manifest()))

            addonInstaller.DRY_RUN = True
            with self.assertRaisesRegex(RuntimeError, "Dry-run must not create extraction directories"):
                addonInstaller.extract_archive_to_temp(archive)

    def test_process_archive_dry_run_reads_manifest_without_extraction(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            archive = root / "pack.mcpack"
            server_dir = root / "server"
            with zipfile.ZipFile(archive, "w") as z:
                z.writestr("manifest.json", json.dumps(self.make_manifest()))

            addonInstaller.DRY_RUN = True
            with (
                mock.patch.object(addonInstaller, "extract_archive_to_temp") as extract_mock,
                mock.patch.object(addonInstaller, "ui_status"),
                mock.patch.object(addonInstaller, "action"),
                mock.patch.object(addonInstaller, "ui_phase"),
                mock.patch.object(addonInstaller, "ui_subitem"),
            ):
                installed, worlds = addonInstaller.process_archive(archive, server_dir)

            extract_mock.assert_not_called()
            self.assertEqual(worlds, [])
            self.assertEqual(len(installed), 1)
            self.assertEqual(installed[0]["pack_id"], PACK_UUID)
            self.assertEqual(installed[0]["kind"], "rp")
            self.assertFalse((ROOT / ".temp-addonInstaller" / archive.name).exists())

    def test_world_ordered_pack_rows_follow_world_json_order(self):
        with tempfile.TemporaryDirectory() as td:
            server_dir = Path(td) / "server"
            world_dir = server_dir / "worlds" / "Bedrock level"
            bp_dir = server_dir / "behavior_packs" / "bp_pack"
            rp_dir = server_dir / "resource_packs" / "rp_pack"
            world_dir.mkdir(parents=True)
            bp_dir.mkdir(parents=True)
            rp_dir.mkdir(parents=True)

            bp_manifest = self.make_manifest(kind="data")
            bp_manifest["header"]["name"] = "Behavior One"
            rp_manifest = self.make_manifest(kind="resources")
            rp_manifest["header"]["uuid"] = OTHER_UUID
            rp_manifest["header"]["name"] = "Resource One"
            (bp_dir / "manifest.json").write_text(json.dumps(bp_manifest), encoding="utf-8")
            (rp_dir / "manifest.json").write_text(json.dumps(rp_manifest), encoding="utf-8")
            (world_dir / "world_behavior_packs.json").write_text(
                json.dumps([{"pack_id": PACK_UUID, "version": [1, 0, 0]}]),
                encoding="utf-8",
            )
            (world_dir / "world_resource_packs.json").write_text(
                json.dumps([{"pack_id": OTHER_UUID, "version": [2, 0, 0]}]),
                encoding="utf-8",
            )

            rows = addonInstaller.world_ordered_pack_rows(server_dir, world_dir)

            self.assertEqual([row["kind"] for row in rows], ["bp", "rp"])
            self.assertEqual([row["name"] for row in rows], ["Behavior One", "Resource One"])
            self.assertEqual([row["index"] for row in rows], [1, 1])

    def test_pack_content_label_summarizes_pack_kinds(self):
        self.assertEqual(addonInstaller.pack_content_label(0, 1), "RP only (1)")
        self.assertEqual(addonInstaller.pack_content_label(1, 0), "BP only (1)")
        self.assertEqual(addonInstaller.pack_content_label(1, 1), "BP + RP (1 BP, 1 RP)")
        self.assertEqual(addonInstaller.pack_content_label(0, 0), "no BP/RP packs")

    def test_disable_pack_in_world_backs_up_and_removes_pack_ref(self):
        with tempfile.TemporaryDirectory() as td:
            world_dir = Path(td) / "world"
            world_dir.mkdir()
            config = world_dir / "world_resource_packs.json"
            config.write_text(
                json.dumps(
                    [
                        {"pack_id": PACK_UUID, "version": [1, 0, 0]},
                        {"pack_id": OTHER_UUID, "version": [1, 0, 0]},
                    ]
                ),
                encoding="utf-8",
            )
            pack = {"pack_id": PACK_UUID, "kind": "rp", "name": "Test Pack"}

            addonInstaller.DRY_RUN = False
            changed = addonInstaller.disable_pack_in_world(world_dir, pack)

            self.assertTrue(changed)
            backups = list(world_dir.glob("world_resource_packs.json.bak-*"))
            self.assertEqual(len(backups), 1)
            backup_packs = json.loads(backups[0].read_text(encoding="utf-8"))
            self.assertEqual([p["pack_id"] for p in backup_packs], [PACK_UUID, OTHER_UUID])
            packs = json.loads(config.read_text(encoding="utf-8"))
            self.assertEqual([p["pack_id"] for p in packs], [OTHER_UUID])

    def test_enable_pack_deduplicates_pack_id(self):
        with tempfile.TemporaryDirectory() as td:
            world_dir = Path(td) / "world"
            world_dir.mkdir()
            config = world_dir / "world_resource_packs.json"
            config.write_text(
                json.dumps(
                    [
                        {"pack_id": PACK_UUID, "version": [1, 0, 0]},
                        {"pack_id": OTHER_UUID, "version": [1, 0, 0]},
                    ]
                ),
                encoding="utf-8",
            )
            installed = {
                "pack_id": PACK_UUID,
                "version": [2, 0, 0],
                "kind": "rp",
            }

            addonInstaller.DRY_RUN = False
            path, backup = addonInstaller.enable_pack(world_dir, installed)

            self.assertEqual(path, config)
            self.assertIsNotNone(backup)
            self.assertTrue(Path(backup).exists())
            packs = json.loads(config.read_text(encoding="utf-8"))
            self.assertEqual([p["pack_id"] for p in packs], [OTHER_UUID, PACK_UUID])
            self.assertEqual(packs[-1]["version"], [2, 0, 0])


if __name__ == "__main__":
    unittest.main()
