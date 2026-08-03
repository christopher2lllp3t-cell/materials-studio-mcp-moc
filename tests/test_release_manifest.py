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
        staging_creation = script.index("New-Item -ItemType Directory -Path $stagingFull")
        self.assertLess(layout_guard, staging_creation)
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

    def test_installer_keeps_current_unchanged_unless_explicitly_activated(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "scripts" / "install_release_v1.ps1").read_text(
            encoding="utf-8"
        )
        activation_gate = script.index("if ($Activate)")
        current_deletion = script.index("[System.IO.Directory]::Delete($current)")
        self.assertIn("[switch]$Activate", script)
        self.assertLess(activation_gate, current_deletion)
        self.assertIn("$activated = $false", script)
        self.assertIn("activated = $activated", script)

    def test_installer_uses_an_isolated_staging_directory_and_deployment_roots(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "scripts" / "install_release_v1.ps1").read_text(
            encoding="utf-8"
        )
        staging_creation = script.index("New-Item -ItemType Directory -Path $stagingFull")
        final_move = script.index("Move-Item -LiteralPath $installFull -Destination $targetFull")
        self.assertLess(staging_creation, final_move)
        self.assertIn('$env:MATERIALS_STUDIO_MCP_ROOT = $installFull', script)
        self.assertIn('$env:MS_MOC_MCP_ROOT = $installFull', script)

    def test_release_builder_excludes_generated_python_artifacts(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "scripts" / "build_release_v1.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn("Generated Python artifacts must not be bundled", script)
        self.assertIn("__pycache__", script)
        self.assertIn(".egg-info", script)

    def test_candidate_verifier_requires_an_unactivated_read_only_deployment(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "scripts" / "verify_candidate_p5a.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn('or $installReceipt.activated', script)
        self.assertIn('Candidate verification must not activate current', script)
        self.assertIn('Candidate read-only preflight wrote to the deployment', script)
        self.assertIn('$bundleRoot', script)
        self.assertIn('@(Get-ForbiddenProcesses)', script)
        self.assertIn('Candidate current-switch script is missing', script)

    def test_current_switch_and_rollback_require_explicit_confirmation(self) -> None:
        root = Path(__file__).resolve().parents[1] / "scripts"
        switch = (root / "switch_current_release_v1.ps1").read_text(encoding="utf-8")
        rollback = (root / "rollback_release_v1.ps1").read_text(encoding="utf-8")
        confirmation = switch.index("if (-not $ConfirmSwitch)")
        current_move = switch.index("Move-Item -LiteralPath $current -Destination $backup")
        self.assertIn("[switch]$ConfirmSwitch", switch)
        self.assertLess(confirmation, current_move)
        self.assertIn("Move-Item -LiteralPath $backup -Destination $current", switch)
        self.assertIn("[switch]$ConfirmRollback", rollback)
        self.assertIn("switch_current_release_v1.ps1", rollback)

    def test_example_mcp_config_declares_both_deployment_roots(self) -> None:
        config_path = Path(__file__).resolve().parents[1] / "mcp-config.example.json"
        server = json.loads(config_path.read_text(encoding="utf-8"))["mcpServers"]["materials-studio-2023"]
        self.assertEqual(server["cwd"], r"E:\ms_mcp\deployments\current")
        self.assertEqual(server["env"]["MATERIALS_STUDIO_MCP_ROOT"], server["cwd"])
        self.assertEqual(server["env"]["MS_MOC_MCP_ROOT"], server["cwd"])

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
