from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from materials_studio_mcp import server
from materials_studio_mcp.adaptive_planning import build_adaptive_calculation_plan


WORKSPACE_TMP = Path(r"D:\分子动力学模拟\tmp")


def write_periodic_xsd(path: Path) -> str:
    path.write_text(
        "<XSD>"
        '<Atom3d ID="1" Components="Si" XYZ="0,0,0" />'
        '<Atom3d ID="2" Components="O" XYZ="0.25,0.25,0.25" />'
        '<SpaceGroup ITNumber="1" GroupName="P1" '
        'Operators="1,0,0,0,0,1,0,0,0,0,1,0" '
        'AVector="5,0,0" BVector="0,5,0" CVector="0,0,5" />'
        + " " * 300
        + "</XSD>",
        encoding="utf-8",
    )
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


class AdaptiveCalculationPlanningTests(unittest.TestCase):
    def test_runtime_preflight_plan_never_allows_execution(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_TMP) as temporary:
            source = Path(temporary) / "mineral.xsd"
            digest = write_periodic_xsd(source)
            plan = build_adaptive_calculation_plan(
                request="Run a CASTEP runtime preflight",
                input_structure=str(source),
                calculation_context={"schema_version": 1, "input_sha256": digest},
            )
            self.assertEqual(plan["status"], "ready_for_runtime_preflight")
            self.assertFalse(plan["execution_allowed"])
            self.assertEqual(plan["structure_facts"]["runtime_atom_count"], 2)
            self.assertEqual(plan["settings"]["Quality"], "Express")
            self.assertEqual(plan["settings"]["UseCustomEnergyCutoff"], "No")
            self.assertIn(
                "CASTEP_CALCULATION_CAPABILITY_UNVERIFIED",
                {item["code"] for item in plan["execution_blockers"]},
            )

    def test_explicit_context_changes_settings_without_material_specific_defaults(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_TMP) as temporary:
            source = Path(temporary) / "mineral.xsd"
            digest = write_periodic_xsd(source)
            context = {
                "schema_version": 1,
                "engine": {"value": "CASTEP", "source": "user objective"},
                "task": "geometry_optimization",
                "purpose": "preliminary",
                "input_sha256": digest,
                "electronic_character": {"value": "insulator", "source": "reviewed mineral identity"},
                "magnetism": {"value": "nonmagnetic", "source": "reviewed mineral identity"},
                "cell_optimization": {"value": "full", "source": "user requested lattice relaxation"},
                "dispersion": {"value": "off", "source": "no dispersion model requested"},
                "accuracy": "Coarse",
                "kpoint_derivation": "Quality",
                "kpoint_quality": "Coarse",
                "cores": 4,
            }
            plan = build_adaptive_calculation_plan(
                request="Preliminary CASTEP geometry optimization",
                input_structure=str(source),
                calculation_context=context,
            )
            self.assertEqual(plan["preflight_blockers"], [])
            self.assertEqual(plan["settings"]["SpinTreatment"], "Non-polarized")
            self.assertEqual(plan["settings"]["CellOptimization"], "Full")
            self.assertEqual(plan["settings"]["UseDFTD"], "No")
            self.assertEqual(plan["settings"]["KPointDerivation"], "Quality")
            self.assertIsNone(plan["settings"]["Smearing"])

    def test_research_plan_requires_convergence_evidence(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_TMP) as temporary:
            source = Path(temporary) / "mineral.xsd"
            digest = write_periodic_xsd(source)
            plan = build_adaptive_calculation_plan(
                request="Research CASTEP geometry optimization",
                input_structure=str(source),
                calculation_context={
                    "schema_version": 1,
                    "input_sha256": digest,
                    "purpose": "research",
                    "electronic_character": "insulator",
                    "magnetism": "nonmagnetic",
                    "cell_optimization": "full",
                },
            )
            self.assertIn(
                "CONVERGENCE_EVIDENCE_REQUIRED",
                {item["code"] for item in plan["preflight_blockers"]},
            )
            self.assertEqual(plan["status"], "blocked")

    def test_recommender_marks_blocked_plan_and_next_step_consistently(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_TMP) as temporary:
            source = Path(temporary) / "mineral.xsd"
            digest = write_periodic_xsd(source)
            result = server.ms_recommend_workflow(
                request="Research CASTEP geometry optimization",
                input_structure=str(source),
                calculation_context={
                    "schema_version": 1,
                    "input_sha256": digest,
                    "purpose": "research",
                    "cell_optimization": "full",
                    "convergence_evidence": [],
                },
            )
            plan = result["adaptive_calculation_plan"]
            step = result["suggested_next_steps"][0]
            self.assertEqual(plan["status"], "blocked")
            self.assertEqual(step["plan_status"], "blocked")
            self.assertEqual(step["mode"], "blocked")
            self.assertEqual(step["required_before_call"], plan["preflight_blockers"])
            self.assertFalse(step["execution_allowed"])

    def test_existing_recommender_routes_castep_through_adaptive_plan(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_TMP) as temporary:
            source = Path(temporary) / "mineral.xsd"
            digest = write_periodic_xsd(source)
            result = server.ms_recommend_workflow(
                request="CASTEP runtime preflight",
                input_structure=str(source),
                calculation_context={"schema_version": 1, "input_sha256": digest},
            )
            self.assertEqual(result["recommended_workflows"][0]["tool"], "ms_prepare_castep_pl_package")
            self.assertEqual(result["suggested_next_steps"][0]["mode"], "dry_run_only")
            self.assertFalse(result["adaptive_calculation_plan"]["execution_allowed"])

    def test_adaptive_plan_is_hash_and_core_bound_into_generated_package(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_TMP) as temporary:
            root = Path(temporary)
            source = root / "mineral.xsd"
            digest = write_periodic_xsd(source)
            plan = build_adaptive_calculation_plan(
                request="CASTEP runtime preflight for a reviewed nonmagnetic insulator",
                input_structure=str(source),
                calculation_context={
                    "schema_version": 1,
                    "input_sha256": digest,
                    "purpose": "runtime_preflight",
                    "electronic_character": {"value": "insulator", "source": "reviewed test fixture"},
                    "magnetism": {"value": "nonmagnetic", "source": "reviewed test fixture"},
                    "cell_optimization": {"value": "none", "source": "runtime preflight only"},
                    "cores": 4,
                },
            )
            output = root / "adaptive"
            prepared = server.ms_prepare_castep_pl_package(
                input_xsd=str(source),
                input_sha256=digest,
                output_directory=str(output),
                calculation_name="adaptive_mineral",
                spins=[0],
                cores=4,
                adaptive_plan=plan,
                dry_run=False,
            )
            self.assertTrue(prepared["ok"])
            data = prepared["data"]
            self.assertFalse(data["scientific_execution_allowed"])
            self.assertFalse(data["settings"]["legacy_profile_active"])
            self.assertIsNone(data["settings"]["smearing"])
            self.assertEqual(data["settings"]["legacy_values_not_applied"]["smearing"], 0.2)
            script = Path(data["tasks"][0]["pl_path"]).read_text(encoding="utf-8")
            self.assertIn('"Quality" => "Express"', script)
            self.assertIn('"KPointDerivation" => "Quality"', script)
            self.assertIn('"SpinTreatment" => "Non-polarized"', script)
            self.assertIn('"UseDFTD" => "No"', script)
            self.assertNotIn('"Smearing" =>', script)
            instructions = Path(data["manual_submission_path"]).read_text(encoding="utf-8")
            self.assertIn("SUBMISSION BLOCKED BY ADAPTIVE PLAN", instructions)

            changed = dict(plan)
            changed["resources"] = {**plan["resources"], "cores": 8}
            rejected = server.ms_prepare_castep_pl_package(
                input_xsd=str(source), input_sha256=digest,
                output_directory=str(root / "rejected"), calculation_name="mismatch",
                spins=[0], cores=4, adaptive_plan=changed, dry_run=True,
            )
            self.assertFalse(rejected["ok"])

    def test_non_castep_requests_do_not_gain_castep_settings(self) -> None:
        plan = build_adaptive_calculation_plan(
            request="Run a short Forcite molecular dynamics trajectory",
            input_structure=None,
        )
        self.assertEqual(plan["engine"], "Forcite")
        self.assertEqual(plan["status"], "not_applicable")
        self.assertNotIn("settings", plan)

    def test_unknown_context_fields_fail_closed(self) -> None:
        with self.assertRaises(ValueError):
            build_adaptive_calculation_plan(
                request="CASTEP preflight",
                input_structure=None,
                calculation_context={"schema_version": 1, "invented_setting": "yes"},
            )


if __name__ == "__main__":
    unittest.main()
