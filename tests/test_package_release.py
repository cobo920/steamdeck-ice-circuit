# SPDX-License-Identifier: GPL-3.0-or-later
"""Release boundaries and deterministic archive metadata, without device I/O."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("package_release", ROOT / "tools/package_release.py")
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


class ReleaseTests(unittest.TestCase):
    def test_allowlist_omits_unknown_root_secrets_symlinks_and_build_outputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            allowed = ["README.md", "START-HERE.html", "src/example.py", "assets/example.svg", "Install Ice Circuit.desktop", ".github/workflows/test.yml", ".github/workflows/release.yml"]
            omitted = ["private.txt", "secret.py", "dist/test.py", "src/__pycache__/cache.py", "src/build/generated.py", "assets/disk.img", "assets/vendor.efi", "docs/private.key", "docs/.env.txt", "docs/secrets.txt", "assets/.private/record.json", "tools/backup.py.bak", ".github/workflows/unknown.yml", ".github/secrets.txt", ".private/file.py"]
            for name in allowed + omitted:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("fixture")
            (root / "src/linked.py").symlink_to(root / "src/example.py")
            (root / "src/linked-folder").symlink_to(root / "assets", target_is_directory=True)
            self.assertEqual(set(release.collect_sources(root)), set(allowed))

    def test_workflow_allowlist_does_not_follow_a_linked_parent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            external = Path(temporary) / "outside"
            root.mkdir()
            (external / "workflows").mkdir(parents=True)
            (external / "workflows/test.yml").write_text("fixture")
            (root / ".github").symlink_to(external, target_is_directory=True)
            self.assertNotIn(".github/workflows/test.yml", release.collect_sources(root))

    def test_private_identifiers_and_keys_are_rejected_without_echoing_values(self):
        sensitive = [
            "/home/" + "personal-account/work/file.py",
            "192." + "168.77.88",
            "abcd1234-" + "5678-9012-3456-abcdef123456",
            "1A2B-" + "3C4D",
            "-----BEGIN " + "OPENSSH PRIVATE KEY-----",
            "ssh-ed25519 " + "A" * 40,
        ]
        for text in sensitive:
            with self.subTest(kind=text.split()[0][:10]):
                with self.assertRaises(release.PackageError) as error:
                    release.audit_text("docs/example.md", text)
                self.assertNotIn(text, str(error.exception))
        # Product/default paths and explicit fixtures should not be flagged.
        release.audit_text("docs/example.md", "/home/deck/Downloads/ice-circuit /home/.ice-circuit/backups 0000-0000 1111-AAAA 192.0.2.1")

    def test_archive_is_reproducible_and_contains_verifiable_source_manifest(self):
        files = {"src/entry.py": b"print('fixture')\n", "README.md": b"fixture\n", "Install Ice Circuit.desktop": b"fixture\n"}
        manifest = {"schema_version": 1, "files": {name: {"sha256": hashlib.sha256(data).hexdigest()} for name, data in files.items()}}
        first, manifest_data = release.create_archive(files, "ice-circuit-fixture", manifest)
        second, _ = release.create_archive(dict(reversed(list(files.items()))), "ice-circuit-fixture", manifest)
        self.assertEqual(first, second)
        with tarfile.open(fileobj=io.BytesIO(first), mode="r:gz") as archive:
            members = archive.getmembers()
            self.assertTrue(all(member.isfile() and member.mtime == 0 and member.uid == member.gid == 0 for member in members))
            self.assertTrue(all(member.uname == member.gname == "" for member in members))
            self.assertEqual(archive.getmember("ice-circuit-fixture/Install Ice Circuit.desktop").mode, 0o755)
            self.assertEqual(archive.getmember("ice-circuit-fixture/README.md").mode, 0o644)
            packed = {member.name.removeprefix("ice-circuit-fixture/"): archive.extractfile(member).read() for member in members}
        self.assertEqual(packed["RELEASE-MANIFEST.json"], manifest_data)
        self.assertEqual(json.loads(manifest_data), manifest)
        for line in packed["SHA256SUMS"].decode().splitlines():
            expected, name = line.split("  ", 1)
            self.assertEqual(hashlib.sha256(packed[name]).hexdigest(), expected)

    def test_asset_tampering_and_missing_font_notice_abort_packaging(self):
        files = release.collect_sources(ROOT)
        # The frontend may still be generated independently during development.
        for name in release.LAUNCHERS | release.FRONTEND_FILES:
            files.setdefault(name, b"# fixture launcher\n")
        release.validate_sources(files)
        changed = dict(files)
        changed["assets/theme/icons/windows.png"] += b"fixture"
        with self.assertRaisesRegex(release.PackageError, "Asset SHA256/size mismatch"):
            release.validate_sources(changed)
        changed = dict(files)
        changed["assets/theme/FONT-LICENSE.txt"] = b"truncated"
        with self.assertRaisesRegex(release.PackageError, "font notice differs"):
            release.validate_sources(changed)

    def test_zip_matches_tar_has_fixed_dates_and_launcher_permissions(self):
        files = {"README.md": b"fixture\n", "START-HERE.html": b"<p>fixture</p>", "Install Ice Circuit.desktop": b"fixture\n", "ice-circuit.py": b"fixture\n"}
        manifest = {"schema_version": 2, "files": {name: {"sha256": release.sha256(data)} for name, data in files.items()}}
        first = release.create_zip(files, "ice-circuit-fixture", manifest)
        second = release.create_zip(dict(reversed(list(files.items()))), "ice-circuit-fixture", manifest)
        self.assertEqual(first, second)
        with zipfile.ZipFile(io.BytesIO(first)) as archive:
            self.assertIsNone(archive.testzip())
            self.assertEqual(archive.namelist(), sorted(archive.namelist()))
            for info in archive.infolist():
                self.assertEqual(info.date_time, (1980, 1, 1, 0, 0, 0))
                self.assertEqual(info.create_system, 3)
                self.assertTrue(stat.S_ISREG(info.external_attr >> 16))
            for name in release.LAUNCHERS:
                mode = archive.getinfo("ice-circuit-fixture/" + name).external_attr >> 16
                self.assertEqual(stat.S_IMODE(mode), 0o755)
            self.assertEqual(stat.S_IMODE(archive.getinfo("ice-circuit-fixture/README.md").external_attr >> 16), 0o644)
            zipped = {name: archive.read(name) for name in archive.namelist()}
        tar_data, _ = release.create_archive(files, "ice-circuit-fixture", manifest)
        with tarfile.open(fileobj=io.BytesIO(tar_data), mode="r:gz") as archive:
            self.assertEqual(zipped, {info.name: archive.extractfile(info).read() for info in archive.getmembers()})

    def test_release_checksums_cover_both_formats_and_extracted_cli_runs(self):
        files = release.collect_sources(ROOT)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            # Package exactly the shipped sources without creating device data.
            with patch.object(release, "collect_sources", return_value=files):
                first = release.package(root, release.DEFAULT_VERSION)
                before = {p.name: p.read_bytes() for p in (root / "dist").iterdir()}
                second = release.package(root, release.DEFAULT_VERSION)
            self.assertEqual(first, second)
            self.assertEqual(before, {p.name: p.read_bytes() for p in (root / "dist").iterdir()})
            expected_zip = f"Ice-Circuit-SteamDeck-{release.DEFAULT_VERSION}.zip"
            self.assertEqual(first["recommended_download"], expected_zip)
            manifest = json.loads(before[first["manifest"]])
            self.assertEqual(set(manifest["archives"]), {expected_zip, f"ice-circuit-{release.DEFAULT_VERSION}.tar.gz"})
            for name, info in manifest["archives"].items():
                self.assertEqual(info["sha256"], release.sha256(before[name]))
                self.assertEqual(info["size"], len(before[name]))
            for line in before["SHA256SUMS"].decode().splitlines():
                expected, name = line.split("  ", 1)
                self.assertEqual(release.sha256(before[name]), expected)
            extracted = root / "with spaces" / "extracted"
            with zipfile.ZipFile(io.BytesIO(before[expected_zip])) as archive:
                self.assertTrue(all(not info.is_dir() and not Path(info.filename).is_absolute() and ".." not in Path(info.filename).parts for info in archive.infolist()))
                archive.extractall(extracted)
            packaged = extracted / ("ice-circuit-" + release.DEFAULT_VERSION)
            for line in (packaged / "SHA256SUMS").read_text().splitlines():
                expected, name = line.split("  ", 1)
                self.assertEqual(release.sha256((packaged / name).read_bytes()), expected)
            result = subprocess.run([sys.executable, "-I", str(packaged / "ice-circuit.py"), "--help"],
                                    cwd=root, capture_output=True, text=True, check=True, timeout=10)
            self.assertIn("diagnose", result.stdout)
            self.assertIn("rollback", result.stdout)

    def test_workflows_use_pinned_actions_and_release_only_creates_drafts(self):
        import re
        for name in release.WORKFLOW_FILES:
            text = (ROOT / name).read_text()
            uses = re.findall(r"uses:\s*(\S+)", text)
            self.assertTrue(uses)
            self.assertTrue(all(re.fullmatch(r"actions/[a-z-]+@[a-f0-9]{40}", action) for action in uses))
            self.assertNotIn("pull_request_target", text)
            known_steps = set(re.findall(r"\bid:\s*([a-z][a-z0-9_-]*)", text))
            for expression in re.findall(r"\$\{\{(.*?)\}\}", text):
                roots = set(re.findall(r"(?<![.\w])([a-z][a-z0-9_]*)\.", expression))
                self.assertTrue(roots <= {"github", "inputs", "matrix", "steps", "needs", "runner"}, expression)
                for step in re.findall(r"\bsteps\.([a-z][a-z0-9_-]*)\.outputs\.", expression):
                    self.assertIn(step, known_steps, expression)
        text = (ROOT / ".github/workflows/release.yml").read_text()
        self.assertIn("--draft --prerelease", text)
        self.assertIn("--verify-tag", text)
        self.assertNotIn("gh release edit", text)
        for key in ("version", "tag", "commit"):
            self.assertIn(f"{key}: ${{{{ steps.version.outputs.{key} }}}}", text)


if __name__ == "__main__":
    unittest.main()
