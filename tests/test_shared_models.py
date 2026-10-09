"""Shared catalog, persistent selection and repacked-ZIP identity regression tests."""
import io
import os
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "backend"))
import config
import model_store
import shared_models


def archive(files, compression=zipfile.ZIP_STORED, reverse=False):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", compression=compression) as z:
        for name, value in (list(files.items())[::-1] if reverse else files.items()):
            z.writestr(name, value)
    return data.getvalue()


class SharedModelsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = os.path.join(self.tmp.name, "a", "demo_wrk")
        os.makedirs(self.ws)
        self.root = os.path.join(self.tmp.name, "libraries")
        self.cfg = {"model_library_root": self.root}
        self.env = patch.dict(os.environ, {"ADS_AGENT_CONFIG": os.path.join(self.tmp.name, "config.ini"),
                                           "ADS_AGENT_LIBRARY_ROOT": ""})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.files = {
            "kit/lib.defs": "DEFINE Vendor ./circuit\n",
            "kit/circuit/ael/Family.ael": 'create_item("Family", "Inductor");',
            "kit/circuit/ael/Family_list.ael": 'create_constant_form("ABC123", "ABC123",68,"1","ABC123");',
            "kit/circuit/models/data.dat": "1 2 3\n"}

    def save(self, data, filename="test.zip"):
        r = model_store.save_archive(self.ws, filename, data=data)
        return model_store.scan_archive(model_store.store_root(self.ws), r["package_id"])

    def test_first_root_is_persistent_across_parent_directories(self):
        cfg = {"model_library_root": ""}
        first = shared_models.library_root(cfg, self.ws)
        other = os.path.join(self.tmp.name, "different", "other_wrk")
        second = shared_models.library_root({"model_library_root": ""}, other)
        self.assertEqual(first, second)
        self.assertEqual(first, os.path.join(os.path.dirname(self.ws), "libraries"))
        self.assertEqual(config.load()["model_library_root"], first)

    def test_environment_override_does_not_persist(self):
        with patch.dict(os.environ, {"ADS_AGENT_LIBRARY_ROOT": self.root}):
            self.assertEqual(shared_models.library_root({}, self.ws), self.root)
            self.assertFalse(os.path.exists(os.environ["ADS_AGENT_CONFIG"]))

    def test_config_update_preserves_credentials_comments_and_other_sections(self):
        path = os.environ["ADS_AGENT_CONFIG"]
        original = "# comment\n[llm]\napi_key = test-secret\n[models]\nlibrary_root = \n[other]\nx = 42\n"
        with open(path, "w", encoding="utf-8") as f:
            f.write(original)
        config.persist_model_library_root(self.root)
        with open(path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("# comment", content)
        self.assertIn("api_key = test-secret", content)
        self.assertIn("x = 42", content)
        replacement = os.path.join(self.tmp.name, "new-library")
        shared_models.set_library_root(self.cfg, replacement)
        self.assertEqual(config.load()["model_library_root"], replacement)

    def test_repacked_zip_reuses_one_canonical_archive(self):
        original = self.save(archive(self.files))
        first = shared_models.backup_package(self.cfg, self.ws, original)
        repack = self.save(archive(self.files, zipfile.ZIP_DEFLATED, True), "renamed.zip")
        self.assertNotEqual(original["sha256"], repack["sha256"])
        second = shared_models.backup_package(self.cfg, self.ws, repack)
        self.assertEqual(first["package_id"], second["package_id"])
        self.assertTrue(second["reused"])
        self.assertEqual(len(model_store.list_packages(self.root)), 1)
        zips = [n for _p, _d, names in os.walk(self.root) for n in names if n.endswith(".zip")]
        self.assertEqual(len(zips), 1)
        # Both original uploads survive in the source workspace.
        self.assertEqual(len(model_store.list_packages(model_store.store_root(self.ws))), 2)
        target = os.path.join(self.tmp.name, "b", "other_wrk")
        copied = shared_models.copy_to_workspace(self.cfg, target, first["package_id"])
        self.assertEqual(copied["sha256"], original["sha256"])

    def test_changed_model_content_is_never_deduplicated(self):
        first = shared_models.backup_package(self.cfg, self.ws, self.save(archive(self.files)))
        changed = dict(self.files, **{"kit/circuit/models/data.dat": "1 2 4\n"})
        second = shared_models.backup_package(self.cfg, self.ws, self.save(archive(changed)))
        self.assertNotEqual(first["package_id"], second["package_id"])

    def test_part_search_returns_package_cell_and_evidence(self):
        backed = shared_models.backup_package(self.cfg, self.ws, self.save(archive(self.files)))
        found = shared_models.search_models(self.cfg, self.ws, {"query": "ABC123"})
        self.assertEqual(found["total"], 1)
        hit = found["models"][0]
        self.assertEqual(hit["package_id"], backed["package_id"])
        self.assertEqual(hit["cell"], "Family")
        self.assertEqual(hit["library"], "Vendor")
        self.assertFalse(hit["validated"])
        self.assertTrue(hit["member_path"].endswith("Family_list.ael"))
        packages = shared_models.list_packages(self.cfg, self.ws, {"query": "ABC123"})
        self.assertEqual(packages["total"], 1)

    def test_existing_shared_archives_gain_catalog_lazily(self):
        saved = model_store.save_archive(self.ws, "legacy.zip", data=archive(self.files), storage_root=self.root)
        model_store.scan_archive(self.root, saved["package_id"])
        self.assertEqual(shared_models.search_models(self.cfg, self.ws, {"query": "ABC123"})["total"], 1)
        self.assertTrue(model_store.get_package(self.root, saved["package_id"])["content_sha256"])

    def test_compiled_kit_part_tables_and_declared_library(self):
        files = {
            "kit/lib.defs": "DEFINE Vendor ./Vendor\n",
            "kit/Vendor/eesof_lib.cfg": "DESIGN_KIT_NAME=Vendor\n",
            "kit/circuit/ael/Family.atf": "compiled-data",
            "kit/doc/Vendor/Family.htm": '<table><tr><td>PartNumber</td><td>Value</td></tr>'
                 '<tr><td>XYZ123</td><td>10nH</td></tr></table>'}
        shared_models.backup_package(self.cfg, self.ws, self.save(archive(files)))
        hit = shared_models.search_models(self.cfg, self.ws, {"query": "XYZ123"})["models"][0]
        self.assertEqual(hit["cell"], "Family")
        self.assertEqual(hit["library"], "Vendor")
        self.assertEqual(hit["kind"], "documented_part_number")

    def test_empty_query_rejected_and_kind_filtered(self):
        shared_models.backup_package(self.cfg, self.ws, self.save(archive(self.files)))
        with self.assertRaises(shared_models.SharedModelError):
            shared_models.search_models(self.cfg, self.ws, {})
        self.assertEqual(shared_models.search_models(self.cfg, self.ws,
                         {"query": "ABC123", "package_kind": "touchstone"})["total"], 0)

    def test_bad_legacy_archive_reported_without_blocking_other_backups(self):
        first = shared_models.backup_package(self.cfg, self.ws, self.save(archive(self.files)))
        record = model_store.get_package(self.root, first["package_id"])
        os.remove(shared_models._archive_path(self.root, record))
        changed = dict(self.files, **{"kit/circuit/models/data.dat": "new"})
        second = shared_models.backup_package(self.cfg, self.ws, self.save(archive(changed)))
        self.assertEqual(len(second["unavailable_packages"]), 1)
        found = shared_models.search_models(self.cfg, self.ws, {"query": "ABC123"})
        self.assertEqual(found["total"], 1)
        self.assertEqual(len(found["unavailable_packages"]), 1)

    def test_lock_failure_stops_backup_before_storage(self):
        record = self.save(archive(self.files))
        with patch.object(model_store.ManifestLock, "acquire", return_value=False):
            with self.assertRaises(shared_models.SharedModelError):
                shared_models.backup_package(self.cfg, self.ws, record)
        self.assertEqual(model_store.list_packages(self.root), [])

    def test_concurrent_repacked_imports_share_one_archive(self):
        first = self.save(archive(self.files))
        second = self.save(archive(self.files, zipfile.ZIP_DEFLATED, True), "second.zip")
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(lambda r: shared_models.backup_package(self.cfg, self.ws, r),
                                       [first, second]))
        self.assertEqual(results[0]["package_id"], results[1]["package_id"])
        self.assertEqual(len(model_store.list_packages(self.root)), 1)

    def test_concurrent_initial_roots_choose_one_persisted_location(self):
        second = os.path.join(self.tmp.name, "other-parent", "demo_wrk")
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(lambda ws: shared_models.library_root({}, ws), [self.ws, second]))
        self.assertEqual(results[0], results[1])

    def test_unsafe_zip_member_is_rejected_without_shared_storage(self):
        saved = model_store.save_archive(self.ws, "unsafe.zip", data=archive({"../escape": "x"}))
        with self.assertRaises(model_store.ModelStoreError):
            shared_models.backup_package(self.cfg, self.ws, saved)
        self.assertFalse(os.path.isdir(self.root))


if __name__ == "__main__":
    unittest.main()
