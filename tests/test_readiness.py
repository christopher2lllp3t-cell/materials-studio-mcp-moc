from __future__ import annotations

import unittest

from materials_studio_mcp.readiness import audit_research_readiness, load_research_environment, load_workflow_requirements


class ResearchReadinessTests(unittest.TestCase):
    def test_environment_and_requirements_are_strictly_loaded(self) -> None:
        environment = load_research_environment()
        requirements = load_workflow_requirements()
        self.assertEqual(environment["namd"]["execution_enabled"], False)
        self.assertEqual(environment["castep"]["execution_enabled"], False)
        self.assertTrue(requirements["policy"]["manual_gate_never_auto_passes"])

    def test_readiness_separates_ready_and_blocked_workflows(self) -> None:
        result = audit_research_readiness()
        workflows = {item["id"]: item for item in result["workflows"]}
        self.assertEqual(workflows["ms_core_structure"]["status"], "ready")
        self.assertEqual(workflows["lammps_extension"]["status"], "ready")
        self.assertEqual(workflows["forcite_kerogen"]["status"], "blocked")
        self.assertEqual(workflows["castep_dft"]["status"], "blocked")
        self.assertEqual(workflows["results_and_reports"]["status"], "blocked")
        self.assertEqual(workflows["namd_ch03"]["status"], "blocked")
        self.assertEqual(workflows["refprop_eos"]["status"], "ready")
        self.assertEqual(result["execution_allowed_workflows"], ["ms_core_structure", "lammps_extension", "refprop_eos"])


if __name__ == "__main__":
    unittest.main()
