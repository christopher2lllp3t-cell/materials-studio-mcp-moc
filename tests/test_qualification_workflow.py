from __future__ import annotations

from pathlib import Path
import json
import shutil
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from materials_studio_mcp import server
from materials_studio_mcp.public_registry import public_tool_names
from materials_studio_mcp import qualification_workflow as workflow


WORKSPACE_ROOT = Path(r"D:\分子动力学模拟")
G01_INPUT = WORKSPACE_ROOT / "07_mcp_materials_studio" / "mcp_projects" / "g01_v1_reproduction_20260715_r2" / "request" / "official_water.xsd"
PROJECTS_ROOT = WORKSPACE_ROOT / "07_mcp_materials_studio" / "mcp_projects"


class QualificationWorkflowTests(unittest.TestCase):
    def test_profile_root_uses_deployment_discovery(self) -> None:
        from materials_studio_mcp.pipeline_config import PROJECT_ROOT

        self.assertEqual(workflow._ROOT, PROJECT_ROOT)
        self.assertTrue(workflow._PROFILE_PATH.is_file())

    def test_g01_energy_profile_is_closed_and_pcff_only(self) -> None:
        task, settings = server._governed_forcite_profile("energy_pcff_v1", None)
        self.assertEqual(task, "Energy")
        self.assertEqual(settings, {"CurrentForcefield": "pcff", "ChargeAssignment": "Use current"})
        with self.assertRaisesRegex(ValueError, "does not accept"):
            server._governed_forcite_profile("energy_pcff_v1", {"Quality": "Fine"})

    def test_vertical_tool_is_public_and_structured(self) -> None:
        self.assertIn("md_g01_qualification_vertical", public_tool_names())
        tool = server.mcp._tool_manager.get_tool("md_g01_qualification_vertical")
        result = tool.fn(project_id="g01_tool_shape", input_xsd=str(G01_INPUT), projects_root=str(PROJECTS_ROOT), dry_run=True)
        self.assertEqual(result["status"], "dry_run")
        for key in ("artifact_ids", "evidence_ids", "blockers", "next_actions"):
            self.assertIn(key, result)

    def test_dry_run_has_exact_order_and_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT / "tmp") as directory:
            result = server.md_g01_qualification_vertical(
                project_id="g01_vertical_test",
                input_xsd=str(G01_INPUT),
                projects_root=directory,
                dry_run=True,
            )
            self.assertEqual(result["status"], "dry_run")
            self.assertFalse(result["writes_performed"])
            self.assertFalse(result["execution_started"])
            self.assertEqual(
                [item["name"] for item in result["steps"]],
                [
                    "initialize_project", "register_input_xsd", "structure_audit",
                    "forcite_preparation", "forcefield_and_topology_postflight",
                    "forcite_energy", "export_car_mdf", "convert_lammps_data",
                    "lammps_run_0", "lammps_short_minimization", "lammps_nvt_smoke",
                    "vmd_text_validation", "qualification_report",
                ],
            )
            self.assertEqual(result["steps"][2]["status"], "pass")
            self.assertEqual(result["steps"][2]["audit"]["audit"]["atom_count"], 3)
            self.assertEqual(result["steps"][2]["audit"]["audit"]["elements"], {"O": 1, "H": 2})
            self.assertEqual(result["steps"][2]["audit"]["audit"]["bond_count"], 2)
            self.assertEqual(result["steps"][2]["audit"]["audit"]["bond_types"], {"Single": 2})
            self.assertEqual(result["steps"][2]["audit"]["audit"]["formal_charge"], 0.0)
            self.assertFalse((Path(directory) / "g01_vertical_test").exists())

    def test_real_execution_requires_exact_confirmation_and_bounded_parameters(self) -> None:
        with self.assertRaisesRegex(ValueError, "nvt_steps"):
            server.md_g01_qualification_vertical(
                project_id="g01_bad_steps",
                input_xsd=str(G01_INPUT),
                projects_root=str(PROJECTS_ROOT),
                nvt_steps=201,
                dry_run=True,
            )
        with self.assertRaisesRegex(PermissionError, "confirmation_token"):
            server.md_g01_qualification_vertical(
                project_id="g01_no_confirmation",
                input_xsd=str(G01_INPUT),
                projects_root=str(PROJECTS_ROOT),
                dry_run=False,
            )

    def test_mocked_execution_preserves_order_and_writes_qualification_report(self) -> None:
        def fake_checked(tool_name, parameters, *, ttl_seconds):
            project = Path(parameters["project_directory"])
            if tool_name == "ms_forcite_calculation_checked":
                output = project / "model" / "calculations" / f"{parameters['output_slot']}.xsd"
                output.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(parameters["input_structure"], output)
                tree = ET.parse(output)
                for atom in tree.getroot().iter("Atom3d"):
                    if atom.get("ImageOf") is None:
                        atom.set("ForcefieldType", "H_" if atom.get("Components") == "H" else "O_R")
                tree.write(output, encoding="utf-8", xml_declaration=True)
                return {
                    "output_structure": str(output),
                    "forcefield_preparation_audit": {
                        "status": "pass", "atom_count": 3, "bond_count": 2,
                        "forcefield_types": {"O_R": 1, "H_": 2},
                        "missing_forcefield_type_count": 0,
                        "partial_charge_count": 3,
                        "partial_charge_read_error_count": 0,
                        "net_partial_charge_e": 0.0,
                        "net_charge_tolerance_e": 1e-4,
                    },
                    "parsed_report": {"AtomAuditCount": 3, "PartialChargeCount": 3, "PartialChargeReadErrorCount": 0, "NetPartialCharge": 0.0},
                }
            if tool_name == "md_export_xsd_to_car_mdf_checked":
                car = project / "conversion" / "g01_vertical_car_mdf.car"
                mdf = project / "conversion" / "g01_vertical_car_mdf.mdf"
                car.parent.mkdir(parents=True, exist_ok=True)
                car.write_text("CAR\n", encoding="ascii")
                mdf.write_text("MDF\n", encoding="ascii")
                return {"output_car": str(car), "output_mdf": str(mdf)}
            data = project / "conversion" / "g01_vertical_lammps.data"
            data.write_text("LAMMPS DATA\n", encoding="ascii")
            return {"output_data": str(data)}

        def fake_smoke(data, output, **kwargs):
            output.mkdir(parents=True, exist_ok=False)
            files = {
                "input.data": "DATA\n", "in.g01_vertical": "SCRIPT\n",
                "log.lammps": "MCP_STAGE_RUN0_PASS MCP_STAGE_MINIMIZATION_PASS MCP_STAGE_NVT_PASS\n",
                "trajectory.lammpstrj": "DUMP\n", "final.data": "FINAL\n",
            }
            for name, text in files.items():
                (output / name).write_text(text, encoding="ascii")
            receipt = output / "lammps_qualification_receipt.json"
            receipt.write_text(json.dumps({"status": "pass"}), encoding="utf-8")
            return {"status": "pass", "errors": [], "runtime": {}, "artifacts": {"final_data": {"path": str(output / "final.data"), "sha256": workflow._sha256(output / "final.data")}, "trajectory": {"path": str(output / "trajectory.lammpstrj"), "sha256": workflow._sha256(output / "trajectory.lammpstrj")}}}

        def fake_vmd(data, dump, destination, **kwargs):
            target = Path(destination)
            target.mkdir(parents=True, exist_ok=False)
            evidence = target / "vmd_validation_evidence.json"
            evidence.write_text(json.dumps({"status": "pass"}), encoding="utf-8")
            return {"status": "pass", "runtime": {"executable_name": "mock-vmd"}}

        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT / "tmp") as directory:
            with patch.object(workflow, "_checked_call", side_effect=fake_checked), patch.object(workflow, "_run_lammps_smoke", side_effect=fake_smoke), patch.object(workflow, "validate_vmd_text_trajectory", side_effect=fake_vmd), patch.object(workflow.confirmation_manager, "consume"):
                result = workflow.run_g01_qualification_vertical(
                    project_id="g01_mock_execution",
                    input_xsd=str(G01_INPUT),
                    projects_root=directory,
                    dry_run=False,
                    confirmation_token="mock-confirmation",
                )
            self.assertEqual(result["status"], "qualification_pass", result)
            self.assertTrue(result["qualification_only"])
            self.assertFalse(result["production_science_released"])
            report = json.loads(Path(result["report_path"]).read_text(encoding="utf-8"))
            self.assertEqual(report["gates"]["lammps_nvt_smoke"], "pass")
            self.assertIn("not a paper production trajectory", " ".join(report["limitations"]))
            self.assertTrue(Path(result["v2_manifest_path"]).is_file())

    def test_failed_stage_preserves_failure_log_for_review_and_new_run(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT / "tmp") as directory:
            with patch.object(workflow, "_checked_call", side_effect=RuntimeError("mock Forcite failure")), patch.object(workflow.confirmation_manager, "consume"):
                result = workflow.run_g01_qualification_vertical(
                    project_id="g01_mock_failure",
                    input_xsd=str(G01_INPUT),
                    projects_root=directory,
                    dry_run=False,
                    confirmation_token="mock-confirmation",
                )
            self.assertEqual(result["status"], "blocked")
            failure_log = Path(result["failure"]["failure_log"])
            self.assertTrue(failure_log.is_file())
            failure = json.loads(failure_log.read_text(encoding="utf-8"))
            self.assertEqual(failure["error"]["message"], "mock Forcite failure")
            self.assertTrue(failure["qualification_only"])


if __name__ == "__main__":
    unittest.main()
