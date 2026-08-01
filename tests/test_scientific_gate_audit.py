from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import hashlib
import shutil
import tempfile
import unittest

from materials_studio_mcp import server
from materials_studio_mcp.confirmation import confirmation_manager
from materials_studio_mcp.project_manager import initialize_project


WORKSPACE_ROOT = Path(r"D:\分子动力学模拟")
PROJECT = WORKSPACE_ROOT / "07_mcp_materials_studio" / "mcp_projects" / "g01_v1_reproduction_20260715_r2"
XSD = PROJECT / "request" / "official_water.xsd"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def complete_contract() -> dict:
    digest = sha256(XSD)
    return {
        "schema_version": 1,
        "model_id": "target-audit-test",
        "model_role": "target",
        "source_evidence": {"kind": "literature_and_raw_structure", "path": str(XSD), "sha256": digest, "citation_or_id": "TEST-SOURCE"},
        "structure_file": {"path": str(XSD), "sha256": digest},
        "expected_elements": {"O": 1, "H": 2},
        "cell": {"matrix_angstrom": [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]], "periodic_boundary": ["f", "f", "f"]},
        "surface": {"miller_index": [1, 0, 1], "termination": "explicit target termination", "hydroxylation_rule": "frozen target rule"},
        "charge_compensation": "explicit target charge compensation ledger",
        "fixed_regions": {"definition": "frozen atom selection"},
        "fluid": {"composition": [], "density_g_cm3": None, "temperature_k": 298.15, "pressure_mpa": 0.101325, "not_applicable": True},
        "forcefield": {"name": "reviewed-test-forcefield", "version": "1", "parameter_sources": [{"path": str(XSD), "sha256": digest}]},
        "atom_types_and_charges": {"atom_type_source": "target audit", "partial_charge_source": "target audit", "evidence_ids": ["target-charge"]},
        "electrostatics": {"long_range": "PPPM", "mixing_rule": "geometric", "special_bonds": "lj/coul 0 0 1"},
        "lammps_units": "real",
        "replicas": {"seeds": [101, 202], "repeat_count": 2},
        "trajectory_plan": {"equilibration_steps": 1000, "production_steps": 2000},
    }


class ScientificGateAuditTests(unittest.TestCase):
    def test_missing_frozen_intake_is_blocked_without_writing(self) -> None:
        result = server.md_scientific_gate_audit(str(PROJECT), {}, dry_run=True)
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["release_decision"], "BLOCKED")
        self.assertTrue(result["blockers"])
        self.assertFalse(any(Path(path).exists() for path in result["planned_outputs"]))

    def test_complete_intake_still_requires_target_evidence_and_never_emits_release(self) -> None:
        contract = complete_contract()
        evidence = {
            "charge_audit": {"status": "PASS", "model_role": "calibration", "evidence_ids": ["calibration-charge"]},
            "literature_model_comparison": {"status": "UNVERIFIED", "model_role": "target"},
        }
        result = server.md_scientific_gate_audit(str(PROJECT), contract, evidence, dry_run=True)
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["release_decision"], "BLOCKED")
        statuses = {item["gate_id"]: item["status"] for item in result["gates"]}
        self.assertEqual(statuses["charge_audit"], "BLOCKED")
        self.assertIn(statuses["literature_model_consistency"], {"UNVERIFIED", "BLOCKED"})
        self.assertNotIn("production_ready", result)
        self.assertNotIn("production_science_released", result)

    def test_placeholder_values_do_not_satisfy_frozen_intake(self) -> None:
        contract = complete_contract()
        contract["surface"]["termination"] = "UNRESOLVED"
        contract["forcefield"]["version"] = "TODO"
        result = server.md_scientific_gate_audit(str(PROJECT), contract, {}, dry_run=True)
        intake = next(item for item in result["gates"] if item["gate_id"] == "intake_completeness")
        self.assertEqual(intake["status"], "BLOCKED")
        self.assertTrue(any("surface.termination" in item for item in intake["blockers"]))

    def test_calibration_evidence_cannot_become_target_pass(self) -> None:
        contract = complete_contract()
        evidence = {key: {"status": "PASS", "model_role": "calibration"} for key in ("surface_bilateral_audit", "charge_audit", "forcefield_coverage", "energy_force_equivalence", "lammps_run0", "short_minimization", "short_dynamics", "trajectory_semantics")}
        result = server.md_scientific_gate_audit(str(PROJECT), contract, evidence, dry_run=True)
        self.assertTrue(all(item["status"] == "BLOCKED" for item in result["gates"] if item["gate_id"] in {"surface_bilateral_geometry_chemistry", "charge_audit", "forcefield_coverage", "ms_lammps_energy_force_consistency", "run0", "short_minimization", "short_dynamics", "wrapped_unwrapped_trajectory_semantics"}))

    def test_confirmed_audit_writes_all_four_reports_without_release_claim(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT / "tmp") as directory:
            project = Path(initialize_project("science-audit-write", "Science audit", projects_root=directory)["project_directory"])
            source = project / "request" / "target.xsd"
            shutil.copy2(XSD, source)
            contract = complete_contract()
            digest = sha256(source)
            contract["source_evidence"]["path"] = str(source)
            contract["source_evidence"]["sha256"] = digest
            contract["structure_file"]["path"] = str(source)
            contract["structure_file"]["sha256"] = digest
            contract["forcefield"]["parameter_sources"] = [{"path": str(source), "sha256": digest}]
            params = {"project_directory": str(project), "target_model_contract": contract, "evidence_manifest": {}}
            token = confirmation_manager.issue("md_scientific_gate_audit", params, 300)["confirmation_token"]
            result = server.md_scientific_gate_audit(str(project), contract, {}, dry_run=False, confirmation_token=token)
            self.assertTrue(result["written"])
            for key in ("report_path", "markdown_path", "blocker_register_path", "next_action_plan_path"):
                self.assertTrue(Path(result[key]).is_file())
            report_text = Path(result["report_path"]).read_text(encoding="utf-8")
            self.assertNotIn('"production_ready"', report_text)
            self.assertNotIn('"production_science_released"', report_text)


if __name__ == "__main__":
    unittest.main()
