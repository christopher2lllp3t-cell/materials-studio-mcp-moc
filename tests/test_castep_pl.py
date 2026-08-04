from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from materials_studio_mcp import server
from materials_studio_mcp import castep_pl
from materials_studio_mcp.castep_pl import GENERATOR_REVISION, inspect_xsd, safe_name, validate_settings
from materials_studio_mcp.castep_gateway import inspect_castep_gateway_readiness
from materials_studio_mcp.public_registry import public_tool_names


WORKSPACE_TMP = Path(r"D:\分子动力学模拟\tmp")


def write_xsd(path: Path, atoms: int = 4) -> str:
    path.write_text(
        "<XSD Version='23.1'>" + "<Atom3d ID='1'/>" * atoms + "</XSD>" + " " * 300,
        encoding="utf-8",
    )
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


class CastepPlPackageTests(unittest.TestCase):
    def test_periodic_runtime_count_excludes_images_and_expands_symmetry(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_TMP) as temporary:
            source = Path(temporary) / "periodic.xsd"
            source.write_text(
                "<XSD>"
                '<Atom3d ID="1" Components="Na" XYZ="0,0,0" />'
                '<Atom3d ID="2" Components="Cl" XYZ="0.25,0.25,0.25" />'
                '<Atom3d ID="3" ImageOf="1" />'
                '<Atom3d ID="4" ImageOf="1" />'
                '<Atom3d ID="5" ImageOf="2" />'
                '<SpaceGroup ITNumber="225" GroupName="FM-3M" '
                'Operators="1,0,0,0,0,1,0,0,0,0,1,0:'
                '1,0,0,0.5,0,1,0,0.5,0,0,1,0:'
                '1,0,0,0,0,1,0,0.5,0,0,1,0.5:'
                '1,0,0,0.5,0,1,0,0,0,0,1,0.5" '
                'AVector="5,0,0" BVector="0,5,0" CVector="0,0,5" />'
                + " " * 300
                + "</XSD>",
                encoding="utf-8",
            )
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            metadata = inspect_xsd(source, digest)
            self.assertEqual(metadata["xml_atom3d_entries"], 5)
            self.assertEqual(metadata["independent_atom3d_entries"], 2)
            self.assertEqual(metadata["runtime_atom_count"], 8)
            self.assertEqual(
                metadata["atom_count_method"], "verified_space_group_unit_cell_expansion"
            )

    def test_tool_is_public_but_submission_remains_manual(self) -> None:
        self.assertIn("ms_prepare_castep_pl_package", public_tool_names())
        self.assertIn("ms_castep_preflight_checked", public_tool_names())
        self.assertIn("ms_castep_gateway_readiness", public_tool_names())
        catalog = {item["tool"]: item for item in server.ms_task_catalog()["workflows"]}
        self.assertIn("ms_prepare_castep_pl_package", catalog)
        self.assertIn("ms_castep_preflight_checked", catalog)
        self.assertIn("ms_castep_gateway_readiness", catalog)
        self.assertIn("without selecting a Gateway", catalog["ms_prepare_castep_pl_package"]["description"])
        self.assertIn("exit before Gateway selection", catalog["ms_castep_preflight_checked"]["description"])

    def test_gateway_readiness_reports_queue_and_core_blockers_without_submission(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path(r"E:\ms_mcp\ms_mcp_jobs")) as temporary:
            root = Path(temporary) / "ms"
            jobsdocroot = Path(temporary) / "BIOVIA" / "Gateway" / "root_default"
            (root / "etc" / "Gateway").mkdir(parents=True)
            info_directory = jobsdocroot / "dsd" / "conf"
            info_directory.mkdir(parents=True)
            (root / "etc" / "Gateway" / "gwlocation.cfg").write_text(
                f"jobsdocroot={jobsdocroot}\n", encoding="utf-8"
            )
            (info_directory / "gw-info.sbd").write_text(
                "version=BIOVIA Materials Studio 2023\n"
                "cpucorestotal=12\n"
                "queuingsystem=[none]\n"
                "gpuavailable=no\n",
                encoding="utf-8",
            )
            status = inspect_castep_gateway_readiness(
                root, requested_cores=48, service_probe=lambda name: True
            )
            self.assertEqual(status["status"], "blocked")
            self.assertTrue(status["runtime_preflight_ready"])
            self.assertFalse(status["remote_submission_ready"])
            self.assertFalse(status["local_submission_candidate"])
            self.assertEqual(status["available_local_cores"], 12)
            self.assertEqual(
                {item["code"] for item in status["blockers"]},
                {"REMOTE_QUEUE_NOT_CONFIGURED", "REQUESTED_CORES_EXCEED_LOCAL_CAPACITY"},
            )

    def test_gateway_readiness_accepts_local_mode_without_remote_queue(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path(r"E:\ms_mcp\ms_mcp_jobs")) as temporary:
            root = Path(temporary) / "ms"
            jobsdocroot = Path(temporary) / "BIOVIA" / "Gateway" / "root_default"
            (root / "etc" / "Gateway").mkdir(parents=True)
            info_directory = jobsdocroot / "dsd" / "conf"
            info_directory.mkdir(parents=True)
            (root / "etc" / "Gateway" / "gwlocation.cfg").write_text(
                f"jobsdocroot={jobsdocroot}\n", encoding="utf-8"
            )
            (info_directory / "gw-info.sbd").write_text(
                "cpucorestotal=12\nqueuingsystem=[none]\n", encoding="utf-8"
            )
            status = inspect_castep_gateway_readiness(
                root, requested_cores=12, service_probe=lambda name: True
            )
            self.assertEqual(status["status"], "ready")
            self.assertEqual(status["available_modes"], ["local"])
            self.assertTrue(status["local_submission_candidate"])
            self.assertFalse(status["remote_submission_ready"])
            self.assertEqual(status["blockers"], [])
            self.assertEqual(
                {item["code"] for item in status["remote_blockers"]},
                {"REMOTE_QUEUE_NOT_CONFIGURED"},
            )

    def test_parameter_validation_rejects_injection_nonfinite_and_extreme_values(self) -> None:
        base = {
            "spins": [0], "cores": 48, "cutoff": 326.5, "max_scf_cycles": 500,
            "max_geometry_iterations": 150, "scf_convergence": 0.000002,
            "force_convergence": 0.03, "dispersion_method": "TS", "spin_mode": "fixed",
            "density_mixing_amplitude": 0.05, "spin_mixing_amplitude": 0.08,
            "diis_history": 5, "smearing": 0.2, "optimization_algorithm": "BFGS",
        }
        for overrides in (
            {"dispersion_method": 'TS"; die "INJECTED'},
            {"cutoff": float("nan")},
            {"smearing": float("inf")},
            {"cores": 999999},
            {"spins": [0, 0]},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                validate_settings(**{**base, **overrides})

    def test_dry_run_is_hash_bound_and_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_TMP) as temporary:
            root = Path(temporary)
            source = root / "source.xsd"
            digest = write_xsd(source)
            output = root / "prepared"
            result = server.ms_prepare_castep_pl_package(
                input_xsd=str(source),
                input_sha256=digest,
                output_directory=str(output),
                calculation_name="Co vacancy",
                spins=[1, 3],
                dry_run=True,
            )
            self.assertTrue(result["ok"])
            self.assertFalse(output.exists())
            data = result["data"]
            self.assertEqual(data["status"], "dry_run")
            self.assertFalse(data["automatic_submission"])
            self.assertFalse(data["gateway_selected"])
            self.assertEqual(data["generator"]["revision"], GENERATOR_REVISION)

    def test_generation_copies_exact_xsd_and_renders_guarded_pl(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_TMP) as temporary:
            root = Path(temporary)
            source = root / "source.xsd"
            digest = write_xsd(source, atoms=6)
            output = root / "prepared"
            result = server.ms_prepare_castep_pl_package(
                input_xsd=str(source),
                input_sha256=digest,
                output_directory=str(output),
                calculation_name="Co_C3",
                spins=[1, 3],
                cores=48,
                dry_run=False,
            )
            self.assertTrue(result["ok"])
            data = result["data"]
            self.assertEqual(data["status"], "prepared")
            self.assertFalse(data["automatic_submission"])
            self.assertEqual(len(data["tasks"]), 2)
            manifest = json.loads((output / "package_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["source"]["sha256"], digest)
            for task in data["tasks"]:
                copied = Path(task["xsd_path"])
                script = Path(task["pl_path"]).read_text(encoding="utf-8")
                self.assertEqual(copied.read_bytes(), source.read_bytes())
                self.assertIn("Unexpected atom count", script)
                self.assertIn("Documents->Import($model)", script)
                self.assertNotIn("$Documents->Import($model)", script)
                self.assertIn("MS_CASTEP_PL_PREFLIGHT_ONLY", script)
                self.assertIn("RESULT status=preflight_only", script)
                self.assertIn("LOCAL EXECUTION BLOCKED", script)
                self.assertIn("GeometryOptimization->Run", script)
                self.assertLess(
                    script.index("RESULT status=preflight_only"),
                    script.index("LOCAL EXECUTION BLOCKED"),
                )
                self.assertIn('SaveAs("/$calc/opt.xsd")', script)
                self.assertIn('SaveAs("/$calc/report.txt")', script)
                self.assertNotIn("open(my", script)

    def test_hash_mismatch_and_existing_output_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_TMP) as temporary:
            root = Path(temporary)
            source = root / "source.xsd"
            digest = write_xsd(source)
            bad = server.ms_prepare_castep_pl_package(
                input_xsd=str(source),
                input_sha256="0" * 64,
                output_directory=str(root / "new"),
                calculation_name="case",
                spins=[1],
            )
            self.assertFalse(bad["ok"])
            existing = root / "existing"
            existing.mkdir()
            blocked = server.ms_prepare_castep_pl_package(
                input_xsd=str(source),
                input_sha256=digest,
                output_directory=str(existing),
                calculation_name="case",
                spins=[1],
            )
            self.assertFalse(blocked["ok"])
            self.assertEqual(blocked["error"]["code"], "internal_error")

    def test_source_change_during_copy_fails_closed_and_cleans_output(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_TMP) as temporary:
            root = Path(temporary)
            source = root / "source.xsd"
            digest = write_xsd(source)
            output = root / "prepared"
            original_copy = castep_pl.shutil.copy2

            def changed_copy(src, dst):
                result = original_copy(src, dst)
                Path(dst).write_bytes(Path(dst).read_bytes() + b"changed-after-validation")
                return result

            with patch.object(castep_pl.shutil, "copy2", side_effect=changed_copy):
                result = server.ms_prepare_castep_pl_package(
                    input_xsd=str(source), input_sha256=digest,
                    output_directory=str(output), calculation_name="toctou",
                    spins=[0], dry_run=False,
                )
            self.assertFalse(result["ok"])
            self.assertFalse(output.exists())

    def test_exact_generated_pl_preflight_is_confirmed_and_never_enters_castep(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path(r"E:\ms_mcp\ms_mcp_jobs")) as temporary:
            root = Path(temporary)
            source = root / "source.xsd"
            digest = write_xsd(source, atoms=6)
            package = root / "prepared"
            prepared = server.ms_prepare_castep_pl_package(
                input_xsd=str(source), input_sha256=digest,
                output_directory=str(package), calculation_name="preflight_case",
                spins=[0], cores=48, dry_run=False,
            )
            self.assertTrue(prepared["ok"])
            manifest_sha256 = prepared["data"]["manifest_sha256"]
            task = prepared["data"]["tasks"][0]
            parameters = {
                "package_directory": str(package),
                "package_manifest_sha256": manifest_sha256,
                "task_name": task["task"],
                "timeout_seconds": 120,
            }
            fake_executable = Path(__file__).resolve()
            patches = (
                patch.object(server, "pipeline_health_check", return_value={"status": "ready", "checks": []}),
                patch.object(server, "approved_executable", return_value=fake_executable),
                patch.object(server, "acquire_execution_slot", return_value=nullcontext()),
            )
            with patches[0], patches[1], patches[2]:
                dry_run = server.ms_castep_preflight_checked(**parameters, dry_run=True)
            self.assertTrue(dry_run["ok"])
            self.assertFalse(dry_run["data"]["execution_started"])
            self.assertFalse(Path(dry_run["data"]["planned_outputs"][0]).exists())

            confirmation = server.confirmation_manager.issue(
                "ms_castep_preflight_checked", parameters
            )

            def fake_process(command, *, cwd, timeout_seconds, stdin_path=None, env=None):
                self.assertEqual(env.get("MS_CASTEP_PL_PREFLIGHT_ONLY"), "1")
                self.assertEqual(
                    {env.get(key) for key in ("LC_ALL", "LC_CTYPE", "LANG")},
                    {"C"},
                )
                self.assertEqual(command[1], "-flat")
                stem = command[-1]
                (cwd / f"{stem}.pl.out").write_text(
                    "Validated input: preflight_case_s0.xsd; atoms=6; spin=0; cores=48\n"
                    "RESULT status=preflight_only calculation=preflight_case_s0_48c spin=0 atoms=6\n",
                    encoding="utf-8",
                )
                (cwd / f"{stem}MatStudioLog.htm").write_text(
                    "Completion status: (OK). Exiting MatServer: status OK.", encoding="utf-8"
                )
                return subprocess.CompletedProcess(command, 0, "", ""), False, None, 4321

            with (
                patch.object(server, "pipeline_health_check", return_value={"status": "ready", "checks": []}),
                patch.object(server, "approved_executable", return_value=fake_executable),
                patch.object(server, "acquire_execution_slot", return_value=nullcontext()),
                patch.object(server, "_run_guarded_materialsscript_process", side_effect=fake_process),
            ):
                result = server.ms_castep_preflight_checked(
                    **parameters,
                    dry_run=False,
                    confirmation_token=confirmation["confirmation_token"],
                )
            self.assertTrue(result["ok"])
            data = result["data"]
            self.assertEqual(data["status"], "preflight_pass")
            self.assertFalse(data["gateway_selected"])
            self.assertFalse(data["castep_execution_started"])
            self.assertTrue(Path(data["receipt_path"]).is_file())
            forbidden = [
                path for path in Path(task["directory"]).iterdir()
                if path.suffix.lower() in {".castep", ".cell", ".param"}
                or path.name.lower() in {"opt.xsd", "report.txt"}
            ]
            self.assertEqual(forbidden, [])

    def test_local_package_preflight_accepts_hash_bound_allow_local_manifest(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path(r"E:\ms_mcp\ms_mcp_jobs")) as temporary:
            root = Path(temporary)
            source = root / "source.xsd"
            digest = write_xsd(source, atoms=4)
            package = root / "prepared"
            prepared = server.ms_prepare_castep_pl_package(
                input_xsd=str(source), input_sha256=digest,
                output_directory=str(package), calculation_name="local_preflight",
                spins=[0], cores=12, allow_local=True, dry_run=False,
            )
            self.assertTrue(prepared["ok"])
            task = prepared["data"]["tasks"][0]
            script = Path(task["directory"]) / task["pl_document"]
            self.assertNotIn("LOCAL EXECUTION BLOCKED", script.read_text(encoding="utf-8"))
            parameters = {
                "package_directory": str(package),
                "package_manifest_sha256": prepared["data"]["manifest_sha256"],
                "task_name": task["task"],
                "timeout_seconds": 120,
            }
            with (
                patch.object(server, "pipeline_health_check", return_value={"status": "ready", "checks": []}),
                patch.object(server, "approved_executable", return_value=Path(__file__).resolve()),
            ):
                result = server.ms_castep_preflight_checked(**parameters, dry_run=True)
            self.assertTrue(result["ok"])
            plan = result["data"]["validations"]["package"]
            self.assertTrue(plan["allow_local"])

    def test_names_remain_short_ascii_and_stable(self) -> None:
        value = "计算_" + "very_long_name_" * 10
        self.assertEqual(safe_name(value), safe_name(value))
        self.assertLessEqual(len(safe_name(value)), 40)
        self.assertTrue(safe_name(value).isascii())


if __name__ == "__main__":
    unittest.main()
