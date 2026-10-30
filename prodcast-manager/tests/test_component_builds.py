"""Selective builds must not resolve or publish a complete release."""

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "release"))
from component_builds import COMPONENTS, digest, resolve, selection, stage


class ReadOnlyGitHub:
    def __init__(self):
        self.calls = []

    def request(self, method, path):
        if method != "GET" or "/commits/" not in path:
            raise AssertionError("Selective builds may only resolve commits")
        self.calls.append(path)
        return {"sha": "a" * 40}


class ComponentBuildTests(unittest.TestCase):
    def test_select_only_requested_components_and_normalize_version(self):
        value = selection({"version": "v00.06.007", "app": "true", "ai": "false", "manager": True})
        self.assertEqual(value, {"version": "v0.6.7", "refs": {"app": "main", "manager": "main"}})

    def test_empty_selection_fails(self):
        with self.assertRaisesRegex(ValueError, "at least one"):
            selection({"version": "v0.6.7", "app": "false"})

    def test_invalid_versions_fail_before_resolving(self):
        for version in ("0.6.7", "v0.6", "v0.6.7-rc.1", "v65536.0.0", "v1.2.3\n", "v1.2.3; echo bad"):
            with self.subTest(version=version), self.assertRaises(ValueError):
                selection({"version": version, "app": True})

    def test_reject_unused_unknown_and_malformed_refs(self):
        for refs in ('[]', '{"worker":"main"}', '{"license":"main"}', '{"app":null}', '{"app":""}', '{"app":"a\u005cnfoo"}', '{broken'):
            with self.subTest(refs=refs), self.assertRaises(ValueError):
                selection({"version": "v0.6.7", "app": True, "source_refs": refs})

    def test_resolve_freezes_selected_refs_without_base_tags_or_other_repos(self):
        api = ReadOnlyGitHub()
        selected = selection({"version": "v0.6.7", "app": True, "manager": True, "source_refs": '{"app":"feature/pi-auth"}'})
        snapshot = resolve(api, selected, workflow_sha="b" * 40, run_url="https://example.test/run")
        self.assertEqual(api.calls, [
            "/repos/Byteall-Energy/prodcast-app/commits/feature%2Fpi-auth",
            "/repos/Byteall-Energy/prodcast-release/commits/main",
        ])
        self.assertEqual([row["component"] for row in snapshot["components"]], ["app", "manager"])
        self.assertTrue(all(row["sha"] == "a" * 40 for row in snapshot["components"]))
        self.assertFalse(snapshot["production_accepted"])

    def test_all_five_components_have_platform_and_python(self):
        snapshot = resolve(ReadOnlyGitHub(), selection({"version": "v0.6.7", **dict.fromkeys(COMPONENTS, True)}), workflow_sha="b" * 40, run_url="run")
        self.assertEqual(len(snapshot["components"]), 5)
        manager = next(row for row in snapshot["components"] if row["component"] == "manager")
        self.assertEqual(manager["python"], "3.11.9")
        self.assertEqual(manager["runner"], "windows-2025")

    def test_invalid_remote_sha_fails(self):
        class BadGitHub:
            def request(self, *args):
                return {"sha": "main"}
        with self.assertRaisesRegex(ValueError, "Invalid source SHA"):
            resolve(BadGitHub(), {"version": "v0.6.7", "refs": {"app": "main"}}, workflow_sha="b" * 40, run_url="run")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.destination = self.root / "artifact"
        self.snapshot = resolve(ReadOnlyGitHub(), {"version": "v0.6.7", "refs": {"app": "main"}}, workflow_sha="b" * 40, run_url="run")
        self.payload = self.source / "dist/release"
        self.payload.mkdir(parents=True)
        self.binary = self.payload / "app.tar.gz"
        self.binary.write_bytes(b"example compiled payload")
        self.manifest = {
            "version": "v0.6.7", "repository": "Byteall-Energy/prodcast-app", "source_commit": "a" * 40,
            "artifacts": [{"name": self.binary.name, "bytes": self.binary.stat().st_size, "sha256": digest(self.binary)}],
        }
        self.save_manifest()
        (self.payload / "SHA256SUMS").write_text("existing checksums\n")

    def save_manifest(self):
        (self.payload / "release-manifest.json").write_text(json.dumps(self.manifest))

    def test_stage_preserves_payload_and_records_checksums_and_provenance(self):
        stage(self.snapshot, "app", self.source, self.destination)
        self.assertEqual((self.destination / self.binary.name).read_bytes(), self.binary.read_bytes())
        metadata = json.loads((self.destination / "component-build.json").read_text())
        self.assertEqual(metadata["source"]["sha"], "a" * 40)
        self.assertEqual(metadata["workflow_sha"], "b" * 40)
        for line in (self.destination / "BUILD-SHA256SUMS").read_text().splitlines():
            checksum, name = line.split("  ", 1)
            self.assertEqual(digest(self.destination / name), checksum)

    def test_mismatched_manifest_provenance_fails(self):
        for key in ("version", "repository", "source_commit"):
            old = self.manifest[key]
            self.manifest[key] = "wrong"
            self.save_manifest()
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "frozen source"):
                stage(self.snapshot, "app", self.source, self.destination)
            self.manifest[key] = old
        self.assertFalse(self.destination.exists())

    def test_modified_payload_fails_before_upload(self):
        self.binary.write_bytes(b"corrupted")
        with self.assertRaisesRegex(ValueError, "checksum/size"):
            stage(self.snapshot, "app", self.source, self.destination)

    def test_unlisted_file_is_not_published(self):
        (self.payload / "unexpected.txt").write_text("unexpected build output")
        with self.assertRaisesRegex(ValueError, "unlisted"):
            stage(self.snapshot, "app", self.source, self.destination)

    def test_reject_traversal_and_duplicate_manifest_names(self):
        item = dict(self.manifest["artifacts"][0])
        for name in ("../outside", "dir/file", "dir\\file", "bad\nname", ".."):
            self.manifest["artifacts"] = [{**item, "name": name}]
            self.save_manifest()
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "artifact name"):
                stage(self.snapshot, "app", self.source, self.destination)
        self.manifest["artifacts"] = [item, item]
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, "duplicate"):
            stage(self.snapshot, "app", self.source, self.destination)

    def test_manager_stages_only_versioned_executable(self):
        snapshot = resolve(ReadOnlyGitHub(), {"version": "v0.6.7", "refs": {"manager": "main"}}, workflow_sha="b" * 40, run_url="run")
        payload = self.source / "prodcast-manager/dist"
        payload.mkdir(parents=True)
        (payload / "ProdCast-Manager-v0.6.7.exe").write_bytes(b"exe")
        (payload / "other.exe").write_bytes(b"do not ship")
        stage(snapshot, "manager", self.source, self.destination)
        self.assertEqual({p.name for p in self.destination.iterdir()}, {"ProdCast-Manager-v0.6.7.exe", "component-build.json", "BUILD-SHA256SUMS"})

    def test_missing_manager_executable_fails(self):
        snapshot = resolve(ReadOnlyGitHub(), {"version": "v0.6.7", "refs": {"manager": "main"}}, workflow_sha="b" * 40, run_url="run")
        with self.assertRaisesRegex(ValueError, "Missing"):
            stage(snapshot, "manager", self.source, self.destination)


if __name__ == "__main__":
    unittest.main()
