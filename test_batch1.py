import importlib.util
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch


APP_PATH = Path(__file__).with_name("app.py")
SPEC = importlib.util.spec_from_file_location("addon_app", APP_PATH)
app = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(app)


UUID_A = "11111111-1111-1111-1111-111111111111"
UUID_B = "22222222-2222-2222-2222-222222222222"


def manifest(pack_id, name="Example", version=None):
    return {
        "header": {"uuid": pack_id, "name": name, "version": version or [1, 0, 0]},
        "modules": [{"type": "data", "uuid": pack_id, "version": [1, 0, 0]}],
    }


class BatchOneSafetyTests(unittest.TestCase):
    def test_zip_backslash_manifest_is_detected(self):
        with tempfile.TemporaryDirectory() as temp:
            archive = Path(temp) / "backslash.mcpack"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("behavior_packs\\example\\manifest.json", json.dumps(manifest(UUID_A)))

            manifests = app.load_manifests_from_archive(archive)

        self.assertEqual(len(manifests), 1)
        self.assertTrue(manifests[0][0].endswith("behavior_packs/example/manifest.json"))
        self.assertEqual(manifests[0][1]["header"]["uuid"], UUID_A)

    def test_languages_json_cannot_escape_texts_folder(self):
        with tempfile.TemporaryDirectory() as temp:
            pack_dir = Path(temp) / "pack"
            texts_dir = pack_dir / "texts"
            texts_dir.mkdir(parents=True)
            (texts_dir / "languages.json").write_text(
                json.dumps(["../outside", "\\\\host\\share", "C:drive", "en_US"]),
                encoding="utf-8",
            )
            (texts_dir / "en_US.lang").write_text("pack.name=Safe", encoding="utf-8")
            (Path(temp) / "outside.lang").write_text("pack.name=Unsafe", encoding="utf-8")

            files = app.language_files_for_pack(pack_dir)
            value = app.resolve_pack_text(pack_dir, "pack.name")

        self.assertEqual(files, [texts_dir / "en_US.lang"])
        self.assertEqual(value, "Safe")

    def test_duplicate_batch_destination_is_reported(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            server = root / "server"
            server.mkdir()
            (server / "behavior_packs").mkdir()
            (server / "resource_packs").mkdir()
            first = root / "first" / "Same Name.mcpack"
            second = root / "second" / "Same Name.mcpack"
            first.parent.mkdir()
            second.parent.mkdir()
            first.write_bytes(b"placeholder")
            second.write_bytes(b"placeholder")
            first_manifest = manifest(UUID_A, "Same Name", [1, 0, 0])
            second_manifest = manifest(UUID_B, "Same Name", [1, 0, 0])
            context = {
                "archive_items": {
                    first.resolve(): [("manifest.json", first_manifest)],
                    second.resolve(): [("manifest.json", second_manifest)],
                }
            }

            conflicts = app.scan_install_conflicts([first, second], server, context)

        self.assertTrue(any(conflict["type"] == "batch_duplicate_dest" for conflict in conflicts))

    def test_dependency_version_mismatch_is_reported(self):
        needing = {
            "pack_id": UUID_A,
            "dependencies": [{"uuid": UUID_B, "version": [2, 0, 0]}],
        }
        available = [{"pack_id": UUID_B, "version": [1, 0, 0]}]

        issues = app.check_dependencies([needing], available)

        self.assertEqual(issues, [(needing, needing["dependencies"][0], "version_mismatch")])

    def test_world_copy_failure_registers_rollback_record(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            server = root / "server"
            source_world = root / "template"
            worlds_dir = server / "worlds"
            source_world.mkdir(parents=True)
            worlds_dir.mkdir(parents=True)
            (source_world / "level.dat").write_bytes(b"world")

            def partial_failure(src, dest):
                dest.mkdir(parents=True, exist_ok=True)
                (dest / "partial.txt").write_text("partial", encoding="utf-8")
                raise OSError("simulated world copy failure")

            tracked_worlds = []
            with patch.object(app, "ask", return_value="Imported World"):
                with patch.object(app, "copytree_with_progress", side_effect=partial_failure):
                    with self.assertRaisesRegex(OSError, "simulated world copy failure"):
                        app.import_world_as_new(
                            source_world,
                            worlds_dir,
                            "Imported World",
                            server,
                            transaction_worlds=tracked_worlds,
                        )

            self.assertEqual(len(tracked_worlds), 1)
            app.rollback_install([], tracked_worlds, [])
            self.assertFalse((worlds_dir / "Imported World").exists())

    def test_copy_failure_rolls_back_partial_pack_and_replaced_folder(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            server = root / "server"
            source = root / "source"
            destination = server / "behavior_packs" / "Example v1 BP"
            old_pack = server / "behavior_packs" / "Old Example"
            source.mkdir(parents=True)
            destination.parent.mkdir(parents=True)
            old_pack.mkdir()
            (source / "manifest.json").write_text(json.dumps(manifest(UUID_A)), encoding="utf-8")
            (old_pack / "manifest.json").write_text(json.dumps(manifest(UUID_A)), encoding="utf-8")
            original_copy = app.copytree_with_progress

            def partial_failure(src, dest):
                dest.mkdir(parents=True, exist_ok=True)
                (dest / "partial.txt").write_text("partial", encoding="utf-8")
                raise OSError("simulated copy failure")

            tracked = []
            with patch.object(app, "copytree_with_progress", side_effect=partial_failure):
                with patch.object(app, "yes_no", return_value=True):
                    with self.assertRaisesRegex(OSError, "simulated copy failure"):
                        app.install_pack_dir(source, manifest(UUID_A), server, "Example.mcpack", tracked)

            self.assertEqual(len(tracked), 1)
            self.assertFalse(old_pack.exists())
            app.rollback_install(tracked, [], [])
            self.assertTrue(old_pack.exists())
            self.assertFalse(destination.exists())
            self.assertIsNotNone(original_copy)


if __name__ == "__main__":
    unittest.main()
