from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from materials_studio_mcp.project_manager import (
    _record_verified_quality_gate,
    initialize_project,
    register_artifact,
    set_quality_gate,
    transition_project_status,
    update_model_specification,
    validate_project,
)


WORKSPACE_ROOT = Path(r"D:\分子动力学模拟")


class ProjectManagerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        (WORKSPACE_ROOT / "tmp").mkdir(parents=True, exist_ok=True)

    def test_project_lifecycle_and_artifact_integrity(self) -> None:
        with tempfile.TemporaryDirectory(prefix="md_project_test_", dir=WORKSPACE_ROOT / "tmp") as temporary:
            result = initialize_project("demo project", "Demo", projects_root=temporary)
            project_dir = Path(result["project_directory"])
            self.assertEqual(project_dir.name, "demo_project")
            self.assertEqual(validate_project(str(project_dir))["status"], "incomplete")
            validation = update_model_specification(
                str(project_dir),
                {"composition": [{"name": "water", "count": 100}], "temperature_k": 300, "pressure_mpa": 0.1},
                {"name": "COMPASSIII", "units": "real", "charge_model": "forcefield", "mixing_rule": "sixthpower"},
            )
            self.assertEqual(validation["status"], "incomplete")
            self.assertFalse(validation["production_allowed"])
            self.assertTrue(any("science_contract" in item for item in validation["errors"]))
            artifact = project_dir / "request" / "source.txt"
            artifact.write_text("source", encoding="utf-8")
            registered = register_artifact(str(project_dir), str(artifact), "source_reference", "unit-test")
            self.assertEqual(len(registered["artifact"]["sha256"]), 64)
            self.assertTrue(validate_project(str(project_dir))["artifact_checks"][0]["hash_matches"])

    def test_refuses_overwrite(self) -> None:
        with tempfile.TemporaryDirectory(prefix="md_project_test_", dir=WORKSPACE_ROOT / "tmp") as temporary:
            initialize_project("same", "Same", projects_root=temporary)
            with self.assertRaises(FileExistsError):
                initialize_project("same", "Same", projects_root=temporary)

    def test_nonperiodic_project_axes_are_valid_metadata(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nonperiodic_project_", dir=WORKSPACE_ROOT / "tmp") as temporary:
            project = initialize_project("nonperiodic", "Nonperiodic", projects_root=temporary)["project_directory"]
            result = update_model_specification(
                project,
                {"composition": [{"name": "water", "count": 1}], "temperature_k": 50.0, "periodic_axes": []},
            )
            self.assertFalse(any("periodic_axes" in item for item in result["errors"]))

    def test_caller_cannot_claim_quality_gate_pass(self) -> None:
        with tempfile.TemporaryDirectory(prefix="md_project_test_", dir=WORKSPACE_ROOT / "tmp") as temporary:
            project_dir = Path(initialize_project("gate-pass", "Gate", projects_root=temporary)["project_directory"])
            with self.assertRaises(PermissionError):
                set_quality_gate(str(project_dir), "structure", "pass", {"claimed": True})
            manifest = json.loads((project_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["quality_gates"]["structure"], "pending")

    def test_verified_pass_requires_validator_and_evidence(self) -> None:
        with tempfile.TemporaryDirectory(prefix="md_project_test_", dir=WORKSPACE_ROOT / "tmp") as temporary:
            project_dir = Path(initialize_project("verified-pass", "Gate", projects_root=temporary)["project_directory"])
            with self.assertRaises(ValueError):
                _record_verified_quality_gate(str(project_dir), "structure", validator="", passed=True, evidence={"atoms": 10})
            with self.assertRaises(ValueError):
                _record_verified_quality_gate(
                    str(project_dir), "structure", validator="structure_preflight:v1", passed=True, evidence={}
                )
            result = _record_verified_quality_gate(
                str(project_dir), "structure", validator="structure_preflight:v1", passed=True,
                evidence={"atom_count": 10, "overlap_count": 0},
            )
            self.assertEqual(result["status"], "pass")
            manifest = json.loads((project_dir / "manifest.json").read_text(encoding="utf-8"))
            record = manifest["quality_gate_evidence"]["structure"]
            self.assertEqual(record["source"], "validator:structure_preflight:v1")
            self.assertTrue(record["evidence"]["validator_result"])
            self.assertEqual(len(manifest["quality_gate_history"]["structure"]), 1)

    def test_verified_pass_is_not_downgraded_by_caller(self) -> None:
        with tempfile.TemporaryDirectory(prefix="md_project_test_", dir=WORKSPACE_ROOT / "tmp") as temporary:
            project_dir = Path(initialize_project("immutable-pass", "Gate", projects_root=temporary)["project_directory"])
            _record_verified_quality_gate(
                str(project_dir), "structure", validator="structure_preflight:v1", passed=True,
                evidence={"overlap_count": 0},
            )
            with self.assertRaises(ValueError):
                set_quality_gate(str(project_dir), "structure", "pending", {"reason": "retry"})
            result = _record_verified_quality_gate(
                str(project_dir), "structure", validator="structure_preflight:v1", passed=False,
                evidence={"overlap_count": 2},
            )
            self.assertEqual(result["status"], "fail")

    def test_caller_failure_is_audited(self) -> None:
        with tempfile.TemporaryDirectory(prefix="md_project_test_", dir=WORKSPACE_ROOT / "tmp") as temporary:
            project_dir = Path(initialize_project("failed-gate", "Gate", projects_root=temporary)["project_directory"])
            result = set_quality_gate(str(project_dir), "forcefield", "fail", {"missing_types": ["X1"]})
            self.assertEqual(result["previous_status"], "pending")
            manifest = json.loads((project_dir / "manifest.json").read_text(encoding="utf-8"))
            history = manifest["quality_gate_history"]["forcefield"]
            self.assertEqual(history[-1]["source"], "caller")
            self.assertEqual(history[-1]["status"], "fail")

    def test_project_state_machine_allows_only_adjacent_forward_transitions(self) -> None:
        with tempfile.TemporaryDirectory(prefix="state_machine_", dir=WORKSPACE_ROOT / "tmp") as temporary:
            project = initialize_project("state-machine", "State", projects_root=temporary)["project_directory"]
            with self.assertRaisesRegex(ValueError, "Illegal project status transition"):
                transition_project_status(project, "modelled", reason="skip")
            first = transition_project_status(project, "specified", reason="contract accepted", evidence_ids=["e1"])
            self.assertTrue(first["changed"])
            replay = transition_project_status(project, "specified", reason="retry", evidence_ids=["e1"])
            self.assertFalse(replay["changed"])
            self.assertTrue(replay["replayed"])
            second = transition_project_status(project, "modelled", reason="model artifact registered")
            self.assertEqual(second["previous_status"], "specified")
            manifest = json.loads((Path(project) / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["project"]["status"], "modelled")
            self.assertEqual([item["to"] for item in manifest["project_status_history"]], ["specified", "modelled"])

    def test_failed_state_is_terminal(self) -> None:
        with tempfile.TemporaryDirectory(prefix="state_failed_", dir=WORKSPACE_ROOT / "tmp") as temporary:
            project = initialize_project("failed-terminal", "State", projects_root=temporary)["project_directory"]
            transition_project_status(project, "failed", reason="fatal validation error")
            with self.assertRaisesRegex(ValueError, "Illegal project status transition"):
                transition_project_status(project, "specified", reason="unsafe recovery")


if __name__ == "__main__":
    unittest.main()
