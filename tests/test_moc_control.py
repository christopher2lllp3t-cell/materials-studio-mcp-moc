from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from materials_studio_mcp import moc_control


class MocControlDiscoveryTests(unittest.TestCase):
    def test_discovers_moc_from_same_immutable_deployment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "release-bundle.json").write_text("{}", encoding="utf-8")
            script = root / "moc" / "ms_moc.py"
            script.parent.mkdir()
            script.write_text("# controlled MOC\n", encoding="utf-8")
            module = root / ".venv" / "Lib" / "site-packages" / "materials_studio_mcp" / "moc_control.py"
            module.parent.mkdir(parents=True)
            module.write_text("# installed module\n", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop(moc_control.MOC_SCRIPT_ENV, None)
                self.assertEqual(moc_control.discover_moc_script(module), script.resolve())

    def test_explicit_moc_script_override_wins(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "ms_moc.py"
            script.write_text("# configured MOC\n", encoding="utf-8")
            with patch.dict(os.environ, {moc_control.MOC_SCRIPT_ENV: str(script)}):
                self.assertEqual(moc_control.discover_moc_script(Path("unused")), script.resolve())

    def test_deployment_environment_binds_moc_to_same_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "release-bundle.json").write_text("{}", encoding="utf-8")
            script = root / "moc" / "ms_moc.py"
            script.parent.mkdir()
            script.write_text("# controlled MOC\n", encoding="utf-8")
            with patch.object(moc_control, "MOC_SCRIPT", script):
                environment = moc_control._moc_environment()
            self.assertEqual(environment[moc_control.MOC_MCP_ROOT_ENV], str(root))


if __name__ == "__main__":
    unittest.main()
