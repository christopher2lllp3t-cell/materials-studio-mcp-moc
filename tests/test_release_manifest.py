from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from materials_studio_mcp import __version__
from materials_studio_mcp.public_registry import API_VERSION, public_tool_names
from materials_studio_mcp.release import verify_deployment, verify_release_manifest, write_release_manifest


class ReleaseManifestTests(unittest.TestCase):
    def test_installer_preflights_bundle_layout_before_creating_target(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "scripts" / "install_release_v1.ps1").read_text(
            encoding="utf-8"
        )
        layout_guard = script.index("$requiredBundleDirectories")
        target_creation = script.index("New-Item -ItemType Directory -Path $targetFull")
        self.assertLess(layout_guard, target_creation)
        for required in (
            '"config"',
            '"moc"',
            '"scripts"',
            '"src"',
            '"tests"',
            '"wheelhouse"',
            '"config\\policy.json"',
            '"config\\materialsscript-capabilities.json"',
            '"config\\qualification-profiles.json"',
            '"config\\research-environment.local.json"',
            '"config\\research-workflow-requirements.json"',
            '"moc\\ms_moc.py"',
            '"moc\\science-requirements.lock"',
        ):
            self.assertIn(required, script)

    def test_build_and_verify_release_manifest(self) -> None:
        with tempfile.TemporaryDirectory(dir=r"E:\ms_mcp\ms_mcp_jobs") as directory:
            path = Path(directory) / "release.json"
            written = write_release_manifest(path)
            self.assertEqual(written["manifest"]["release"]["version"], __version__)
            self.assertEqual(written["manifest"]["release"]["api_version"], API_VERSION)
            self.assertEqual(written["manifest"]["public_tool_count"], len(public_tool_names()))
            self.assertFalse(any(".egg-info" in item["label"] for item in written["manifest"]["files"]))
            self.assertTrue(any(item["label"].startswith("tests/") for item in written["manifest"]["files"]))
            verified = verify_release_manifest(path)
            self.assertEqual(verified["status"], "pass")

    def test_tampered_manifest_entry_fails_verification(self) -> None:
        with tempfile.TemporaryDirectory(dir=r"E:\ms_mcp\ms_mcp_jobs") as directory:
            path = Path(directory) / "release.json"
            write_release_manifest(path)
            data = json.loads(path.read_text(encoding="utf-8"))
            data["files"][0]["sha256"] = "0" * 64
            path.write_text(json.dumps(data), encoding="utf-8")
            verified = verify_release_manifest(path)
            self.assertEqual(verified["status"], "fail")
            self.assertTrue(any("missing or changed" in item for item in verified["errors"]))

    def test_incomplete_deployment_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(dir=r"E:\ms_mcp\ms_mcp_jobs") as directory:
            root = Path(directory)
            (root / "release-bundle.json").write_text("{}", encoding="utf-8")
            (root / "release-manifest.json").write_text("{}", encoding="utf-8")
            verified = verify_deployment(root)
            self.assertEqual(verified["status"], "fail")
            self.assertTrue(verified["errors"])


if __name__ == "__main__":
    unittest.main()
