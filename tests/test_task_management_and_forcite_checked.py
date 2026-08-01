from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from materials_studio_mcp import server
from materials_studio_mcp.geology_modeling import sha256_file
from materials_studio_mcp.project_manager import initialize_project
from materials_studio_mcp import task_manager


WORKSPACE_TMP = Path(r"D:\分子动力学模拟\tmp")


class _FakeProcess:
    pid = 424242


class TaskManagementTests(unittest.TestCase):
    def test_task_request_rejects_reserved_fields_and_unknown_tools(self) -> None:
        with self.assertRaisesRegex(ValueError, "not allowed"):
            task_manager.validate_task_request("powershell", {})
        with self.assertRaisesRegex(ValueError, "reserved"):
            task_manager.validate_task_request(
                "ms_forcite_calculation_checked", {"confirmation_token": "secret"}
            )

    def test_submit_persists_only_owner_hash_and_query_requires_token(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mcp_tasks_", dir=WORKSPACE_TMP) as temporary:
            with patch.dict(os.environ, {task_manager.TASK_ROOT_ENV: temporary}), patch.object(
                task_manager, "_launch_worker", return_value={"kind": "test", "worker_pid": 424242}
            ):
                submitted = task_manager.submit_task(
                    "ms_forcite_calculation_checked", {"project_directory": "demo", "idempotency_key": "original"}
                )
                record = json.loads(Path(submitted["task_record"]).read_text(encoding="utf-8"))
                self.assertNotIn(submitted["owner_capability"], json.dumps(record))
                self.assertEqual(record["status"], "running")
                queried = task_manager.query_task(submitted["task_id"], submitted["owner_capability"])
                self.assertEqual(queried["worker_pid"], 424242)
                with self.assertRaises(PermissionError):
                    task_manager.query_task(submitted["task_id"], "wrong")

    def test_retry_requires_terminal_failure_and_creates_linked_task(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mcp_tasks_", dir=WORKSPACE_TMP) as temporary:
            with patch.dict(os.environ, {task_manager.TASK_ROOT_ENV: temporary}), patch.object(
                task_manager, "_launch_worker", return_value={"kind": "test", "worker_pid": 424242}
            ):
                submitted = task_manager.submit_task(
                    "ms_forcite_calculation_checked", {"project_directory": "demo", "idempotency_key": "original"}
                )
                with self.assertRaisesRegex(ValueError, "failed or cancelled"):
                    task_manager.retry_task(submitted["task_id"], submitted["owner_capability"])
                record_path = Path(submitted["task_record"])
                record = json.loads(record_path.read_text(encoding="utf-8"))
                record["status"] = "failed"
                record_path.write_text(json.dumps(record), encoding="utf-8")
                retried = task_manager.retry_task(submitted["task_id"], submitted["owner_capability"])
                retried_record = json.loads(Path(retried["task_record"]).read_text(encoding="utf-8"))
                self.assertEqual(retried_record["retry_of"], submitted["task_id"])
                self.assertRegex(retried_record["parameters"]["idempotency_key"], r"^original\.retry\.[0-9a-f]{12}$")


class GovernedForciteTests(unittest.TestCase):
    def test_forcefield_preparation_profiles_are_closed_and_preserve_bond_orders(self) -> None:
        expected = {
            "prepare_compassiii_v1": ("COMPASSIII", "Forcefield assigned"),
            "prepare_pcff_v1": ("pcff", "Forcefield assigned"),
            "prepare_dreiding_qeq_v1": ("Dreiding", "Charge using QEq"),
            "prepare_universal_qeq_v1": ("Universal", "Charge using QEq"),
        }
        for profile_id, (forcefield, charge_assignment) in expected.items():
            task, settings = server._governed_forcite_profile(profile_id, None)
            self.assertEqual(task, "Energy")
            self.assertEqual(settings["CurrentForcefield"], forcefield)
            self.assertEqual(settings["AssignForcefieldTypes"], "Yes")
            self.assertEqual(settings["AssignBondOrder"], "No")
            self.assertEqual(settings["ChargeAssignment"], charge_assignment)
            with self.assertRaisesRegex(ValueError, "does not accept"):
                server._governed_forcite_profile(profile_id, {"ChargeAssignment": "Use current"})

    def test_forcefield_preparation_preflight_allows_only_missing_types(self) -> None:
        missing = {
            "format": "xsd", "atom_count": 2, "missing_forcefield_type_count": 2,
            "errors": ["2 atoms do not have ForcefieldType; forcefield coverage is incomplete"],
        }
        self.assertTrue(server._typing_input_preflight_is_acceptable(missing))
        self.assertFalse(server._typing_input_preflight_is_acceptable({
            **missing, "errors": [*missing["errors"], "bad geometry"],
        }))

    def test_forcefield_preparation_postflight_checks_types_charges_and_topology(self) -> None:
        input_structure = {
            "atom_count": 2, "bond_count": 1, "elements": {"C": 1, "O": 1},
            "bond_types": {"Double": 1},
        }
        output_structure = {
            **input_structure, "missing_forcefield_type_count": 0,
            "forcefield_types": {"c2": 1, "o1": 1},
        }
        report = {
            "AtomAuditCount": 2, "PartialChargeCount": 2,
            "PartialChargeReadErrorCount": 0, "MissingForcefieldTypeCount": 0,
            "NetPartialCharge": 0.0,
        }
        passed = server._typing_postflight_summary(input_structure, output_structure, report)
        self.assertEqual(passed["status"], "pass")
        failed = server._typing_postflight_summary(
            input_structure, {**output_structure, "bond_types": {"Single": 1}}, report
        )
        self.assertEqual(failed["status"], "fail")
        self.assertTrue(any("bond_types" in item for item in failed["errors"]))

    def test_forcefield_preparation_template_contains_independent_atom_audit(self) -> None:
        _, settings = server._governed_forcite_profile("prepare_compassiii_v1", None)
        script, _ = server._build_forcite_script_template(
            "Energy", settings, include_structure=True, include_trajectory=False
        )
        self.assertIn("AssignForcefieldTypes", script)
        self.assertIn("AtomAuditCount", script)
        self.assertIn("MissingForcefieldTypeCount", script)
        self.assertIn("NetPartialCharge", script)

    def test_task_catalog_marks_fallback_profiles_as_diagnostic(self) -> None:
        profiles = server.ms_task_catalog()["forcite_profiles"]["preparation"]
        by_id = {item["profile_id"]: item for item in profiles}
        self.assertEqual(by_id["prepare_compassiii_v1"]["role"], "primary")
        self.assertEqual(by_id["prepare_pcff_v1"]["role"], "diagnostic_fallback")
        self.assertTrue(all(not item["production_released"] for item in profiles))

    def test_preparation_dry_run_accepts_missing_types_without_writing(self) -> None:
        with tempfile.TemporaryDirectory(prefix="forcite_prepare_", dir=WORKSPACE_TMP) as temporary:
            project = Path(initialize_project("forcite-prepare", "Forcite prepare", projects_root=temporary)["project_directory"])
            source = project / "model" / "input.xsd"
            source.write_text("<XSD />\n", encoding="ascii")
            missing = {
                "format": "xsd", "status": "fail", "atom_count": 2, "bond_count": 1,
                "elements": {"C": 1, "O": 1}, "bond_types": {"Double": 1},
                "missing_forcefield_type_count": 2,
                "errors": ["2 atoms do not have ForcefieldType; forcefield coverage is incomplete"],
            }
            before = {path.relative_to(project) for path in project.rglob("*")}
            with patch.object(server, "inspect_structure_preflight", return_value=missing), patch.object(
                server, "pipeline_health_check", return_value={"status": "ready", "checks": []}
            ):
                result = server.ms_forcite_calculation_checked(
                    str(project), str(source), sha256_file(source), "prepare_compassiii_v1", None,
                    "prepare_dry", "prepare-dry-key", dry_run=True,
                )
            self.assertTrue(result["ok"])
            self.assertEqual(result["data"]["status"], "dry_run")
            self.assertEqual(before, {path.relative_to(project) for path in project.rglob("*")})

    def test_failed_forcite_evidence_is_preserved_and_registered(self) -> None:
        with tempfile.TemporaryDirectory(prefix="forcite_failed_", dir=WORKSPACE_TMP) as temporary:
            project = Path(initialize_project("forcite-failed", "Forcite failed", projects_root=temporary)["project_directory"])
            source = project / "model" / "input.xsd"
            source.write_text("<XSD />\n", encoding="ascii")
            job = Path(temporary) / "job"
            job.mkdir()
            evidence_paths = {}
            for key, name in {
                "rendered_script_path": "script.pl",
                "script_stdout_path": "stdout.log",
                "matstudio_log_path": "matstudio.log",
                "audit_path": "audit.json",
            }.items():
                path = job / name
                path.write_text("evidence", encoding="ascii")
                evidence_paths[key] = str(path)
            result = {
                **evidence_paths, "job_id": "job-1", "job_dir": str(job),
                "error_summary": "typing failed", "outputs": {},
            }
            failure = server._persist_failed_forcite_evidence(
                project_directory=str(project),
                evidence_root=project / "reports" / "typing_failed.forcite",
                source=source,
                source_sha256=sha256_file(source),
                profile_id="prepare_pcff_v1",
                parameters={"output_slot": "typing_failed"},
                module_settings={"CurrentForcefield": "pcff"},
                health={"status": "ready"},
                result=result,
            )
            self.assertEqual(failure["status"], "forcite_execution_failed")
            self.assertTrue(Path(failure["failure_receipt"]).is_file())
            manifest = json.loads((project / "manifest.json").read_text(encoding="utf-8"))
            self.assertTrue(any(item["role"] == "forcite_failure_evidence" for item in manifest["artifacts"]))

    def test_successful_preparation_records_trusted_structure_and_forcefield_gates(self) -> None:
        with tempfile.TemporaryDirectory(prefix="forcite_gates_", dir=WORKSPACE_TMP) as temporary:
            project = Path(initialize_project("forcite-gates", "Forcite gates", projects_root=temporary)["project_directory"])
            typing = {
                "status": "pass", "atom_count": 2, "bond_count": 1,
                "elements": {"C": 1, "O": 1}, "bond_types": {"Double": 1},
                "forcefield_types": {"c2": 1, "o1": 1},
                "missing_forcefield_type_count": 0, "partial_charge_count": 2,
                "partial_charge_read_error_count": 0, "net_partial_charge_e": 0.0,
                "net_charge_tolerance_e": 1.0e-4,
            }
            decisions = server._record_forcefield_preparation_gates(
                project_directory=str(project), profile_id="prepare_compassiii_v1",
                output_sha256="a" * 64, output_preflight={"status": "pass"},
                typing_summary=typing,
            )
            self.assertEqual(decisions["structure"]["status"], "pass")
            self.assertEqual(decisions["forcefield"]["status"], "pass")
            manifest = json.loads((project / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["quality_gates"]["structure"], "pass")
            self.assertEqual(manifest["quality_gates"]["forcefield"], "pass")
            self.assertTrue(manifest["quality_gate_evidence"]["forcefield"]["source"].startswith("validator:"))

    def test_profile_validation_is_closed_and_range_checked(self) -> None:
        task, settings = server._governed_forcite_profile(
            "dynamics_nvt_compassiii_v1",
            {"temperature_kelvin": 298, "number_of_steps": 500, "time_step_fs": 1, "trajectory_frequency": 50},
        )
        self.assertEqual(task, "Dynamics")
        self.assertEqual(settings["Ensemble3D"], "NVT")
        with self.assertRaisesRegex(ValueError, "Unsupported dynamics parameters"):
            server._governed_forcite_profile("dynamics_nvt_compassiii_v1", {"Pressure": 1})
        with self.assertRaisesRegex(ValueError, "number_of_steps"):
            server._governed_forcite_profile("dynamics_nvt_compassiii_v1", {"number_of_steps": 0})

    def test_checked_forcite_dry_run_validates_without_writing(self) -> None:
        with tempfile.TemporaryDirectory(prefix="forcite_checked_", dir=WORKSPACE_TMP) as temporary:
            project = Path(initialize_project("forcite-dry", "Forcite dry", projects_root=temporary)["project_directory"])
            source = project / "model" / "input.xsd"
            source.write_text("<XSD />\n", encoding="ascii")
            before = {path.relative_to(project) for path in project.rglob("*")}
            with patch.object(server, "inspect_structure_preflight", return_value={"status": "pass", "atom_count": 1}), patch.object(
                server, "pipeline_health_check", return_value={"status": "ready", "checks": []}
            ):
                result = server.ms_forcite_calculation_checked(
                    str(project), str(source), sha256_file(source), "energy_compassiii_v1", {},
                    "energy_dry", "energy-dry-key", dry_run=True,
                )
            self.assertTrue(result["ok"])
            self.assertEqual(result["data"]["status"], "dry_run")
            self.assertFalse(result["data"]["writes_performed"])
            self.assertIsNotNone(result["data"]["template_sha256"])
            after = {path.relative_to(project) for path in project.rglob("*")}
            self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
