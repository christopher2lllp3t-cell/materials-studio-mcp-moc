from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from materials_studio_mcp.pipeline_config import load_pipeline_config, resolve_workspace_path
from materials_studio_mcp.project_manager import _write_manifest, initialize_project
from materials_studio_mcp.security import REDACTED, redact_sensitive


ROOT = Path(r"D:\分子动力学模拟")


class PlatformSecurityTests(unittest.TestCase):
    def test_unknown_config_keys_fail_closed(self) -> None:
        config = load_pipeline_config()
        software = config["software"]
        policy = config["policy"]
        with tempfile.TemporaryDirectory(dir=ROOT / "tmp") as temporary:
            root = Path(temporary)
            software_path = root / "software.json"
            policy_path = root / "policy.json"
            bad_software = json.loads(json.dumps(software))
            bad_software["unexpected_command"] = "cmd.exe"
            software_path.write_text(json.dumps(bad_software), encoding="utf-8")
            policy_path.write_text(json.dumps(policy), encoding="utf-8")
            with patch.dict(os.environ, {
                "MD_PIPELINE_SOFTWARE_CONFIG": str(software_path),
                "MD_PIPELINE_POLICY_CONFIG": str(policy_path),
            }, clear=False), self.assertRaisesRegex(ValueError, "Unknown software"):
                load_pipeline_config()

    def test_nested_unknown_policy_keys_fail_closed(self) -> None:
        config = load_pipeline_config()
        with tempfile.TemporaryDirectory(dir=ROOT / "tmp") as temporary:
            root = Path(temporary)
            software_path = root / "software.json"
            policy_path = root / "policy.json"
            bad_policy = json.loads(json.dumps(config["policy"]))
            bad_policy["limits"]["unlimited_jobs"] = True
            software_path.write_text(json.dumps(config["software"]), encoding="utf-8")
            policy_path.write_text(json.dumps(bad_policy), encoding="utf-8")
            with patch.dict(os.environ, {
                "MD_PIPELINE_SOFTWARE_CONFIG": str(software_path),
                "MD_PIPELINE_POLICY_CONFIG": str(policy_path),
            }, clear=False), self.assertRaisesRegex(ValueError, "policy.limits"):
                load_pipeline_config()

    def test_recursive_redaction_covers_fields_and_inline_secrets(self) -> None:
        source = {
            "password": "plain",
            "nested": [{"api_key": "abc", "message": "token=xyz Bearer abc.def"}],
            "safe": "value",
        }
        result = redact_sensitive(source)
        self.assertEqual(result["password"], REDACTED)
        self.assertEqual(result["nested"][0]["api_key"], REDACTED)
        rendered = repr(result)
        for secret in ("plain", "abc", "xyz", "abc.def"):
            self.assertNotIn(secret, rendered)
        self.assertEqual(result["safe"], "value")

    def test_symlink_escape_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "tmp") as temporary:
            base = Path(temporary)
            outside = Path(tempfile.mkdtemp(prefix="mcp_outside_"))
            link = base / "escape"
            try:
                try:
                    link.symlink_to(outside, target_is_directory=True)
                except OSError as exc:
                    if os.name != "nt":
                        self.skipTest(f"Symlink creation is unavailable: {exc}")
                    junction = subprocess.run(
                        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(outside)],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    if junction.returncode != 0:
                        self.skipTest(f"Junction creation is unavailable: {junction.stderr or junction.stdout}")
                with self.assertRaises(PermissionError):
                    resolve_workspace_path(link / "payload.txt")
            finally:
                if link.is_symlink():
                    link.unlink()
                elif link.exists():
                    link.rmdir()
                outside.rmdir()

    def test_atomic_write_failure_preserves_valid_manifest(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "tmp") as temporary:
            path = Path(temporary) / "manifest.json"
            original = {"value": "original"}
            _write_manifest(path, original)
            with patch("materials_studio_mcp.project_manager.os.replace", side_effect=OSError("interrupted")):
                with self.assertRaisesRegex(OSError, "interrupted"):
                    _write_manifest(path, {"value": "new"})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), original)
            self.assertEqual(list(path.parent.glob(".manifest.json.*.tmp")), [])

    def test_manifest_updates_are_serialized_across_processes(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "tmp") as temporary:
            project = Path(initialize_project("concurrent", "Concurrent", projects_root=temporary)["project_directory"])
            manifest = project / "manifest.json"
            code = (
                "import sys,time\n"
                "from pathlib import Path\n"
                "from materials_studio_mcp.project_manager import _locked_manifest\n"
                "p=Path(sys.argv[1]); key=sys.argv[2]\n"
                "with _locked_manifest(p) as m:\n"
                " m.setdefault('concurrency_test', {})[key]=key\n"
                " time.sleep(0.08)\n"
            )
            processes = [
                subprocess.Popen([sys.executable, "-c", code, str(manifest), f"worker-{index}"])
                for index in range(4)
            ]
            for process in processes:
                self.assertEqual(process.wait(timeout=15), 0)
            result = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(set(result["concurrency_test"]), {f"worker-{index}" for index in range(4)})


if __name__ == "__main__":
    unittest.main()
