from __future__ import annotations

import unittest
import tempfile
from pathlib import Path

from materials_studio_mcp import pipeline_config
from materials_studio_mcp.pipeline_config import load_pipeline_config, pipeline_health_check


class PipelineConfigTests(unittest.TestCase):
    def test_project_root_discovery_supports_installed_site_packages_layout(self) -> None:
        with tempfile.TemporaryDirectory(prefix="installed_layout_") as temporary:
            root = Path(temporary)
            config = root / "config"
            config.mkdir()
            for name in ("software.local.json", "policy.json", "project-manifest.template.json"):
                (config / name).write_text("{}", encoding="ascii")
            module = root / ".venv" / "Lib" / "site-packages" / "materials_studio_mcp" / "pipeline_config.py"
            module.parent.mkdir(parents=True)
            module.write_text("", encoding="ascii")
            self.assertEqual(pipeline_config.discover_project_root(module), root.resolve())

    def test_configuration_has_required_sections(self) -> None:
        config = load_pipeline_config()
        self.assertIn("software", config)
        self.assertIn("policy", config)
        for section in ("materials_studio", "lammps", "mpi", "vmd", "packmol"):
            self.assertIn(section, config["software"])

    def test_health_check_is_non_executing_when_probes_disabled(self) -> None:
        result = pipeline_health_check(run_version_probes=False)
        self.assertIn(result["status"], {"ready", "degraded", "not_ready"})
        self.assertIsInstance(result["checks"], list)
        self.assertTrue(any(item["name"] == "LAMMPS" for item in result["checks"]))


if __name__ == "__main__":
    unittest.main()
