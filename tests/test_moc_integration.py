from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from materials_studio_mcp import server
from materials_studio_mcp.project_manager import initialize_project


WORKSPACE_ROOT = Path(r"D:\分子动力学模拟")
MOC_PATH = WORKSPACE_ROOT / "tools" / "ms_moc.py"
BRIDGE_PATH = WORKSPACE_ROOT / "tools" / "ms_mcp_bridge.py"


def _load_moc_module():
    spec = importlib.util.spec_from_file_location("tested_ms_moc", MOC_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load MOC module: {MOC_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_bridge_module():
    spec = importlib.util.spec_from_file_location("tested_ms_mcp_bridge", BRIDGE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load MCP bridge module: {BRIDGE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MocCliSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.moc = _load_moc_module()

    def test_status_is_machine_readable_and_ready(self) -> None:
        status = self.moc.collect_status()
        self.assertEqual(status["schema_version"], 2)
        self.assertEqual(status["summary"]["status"], "ready")
        self.assertEqual(status["summary"]["missing_required"], [])

    def test_document_outside_allowed_roots_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            outside = Path(directory) / "outside.xsd"
            outside.write_text("model", encoding="ascii")
            with self.assertRaisesRegex(ValueError, "outside the allowed MOC roots"):
                self.moc.controlled_document(str(outside))

    def test_unsupported_document_suffix_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT / "tmp") as directory:
            unsupported = Path(directory) / "model.exe"
            unsupported.write_bytes(b"not a model")
            with self.assertRaisesRegex(ValueError, "Unsupported Materials Studio document type"):
                self.moc.controlled_document(str(unsupported))

    def test_live_launch_detaches_all_standard_streams(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT / "tmp") as directory:
            document = Path(directory) / "model.xsd"
            document.write_bytes(b"model")
            args = SimpleNamespace(document=str(document), dry_run=False, json=True)
            process = SimpleNamespace(pid=4321, poll=lambda: None)
            with patch.object(self.moc.subprocess, "Popen", return_value=process) as popen, patch.object(
                self.moc, "emit"
            ), patch.object(self.moc.time, "sleep"), patch.object(
                self.moc, "matstudio_pids", side_effect=[[], [4321], [4321]]
            ):
                exit_code = self.moc.launch(args)
            self.assertEqual(exit_code, 0)
            kwargs = popen.call_args.kwargs
            self.assertIs(kwargs["stdin"], self.moc.subprocess.DEVNULL)
            self.assertIs(kwargs["stdout"], self.moc.subprocess.DEVNULL)
            self.assertIs(kwargs["stderr"], self.moc.subprocess.DEVNULL)
            self.assertTrue(kwargs["close_fds"])
            self.assertEqual(Path(kwargs["cwd"]), self.moc.MATSTUDIO.parent)

    def test_process_query_uses_pinned_tasklist(self) -> None:
        completed = subprocess.CompletedProcess(
            [str(self.moc.TASKLIST)], 0, '"MatStudio.exe","1234","Console","1","10,000 K"\n', ""
        )
        with patch.object(self.moc, "run_command", return_value=completed) as run:
            pids = self.moc.matstudio_pids()
        self.assertEqual(pids, [1234])
        self.assertEqual(run.call_args.args[0][0], str(self.moc.TASKLIST))

    def test_reviewed_wrapper_uses_pinned_powershell(self) -> None:
        args = SimpleNamespace(
            script="run_ac_smoketest.ps1", wrapper_args=[], timeout_seconds=30, json=True
        )
        completed = subprocess.CompletedProcess([], 0, "ok", "")
        with patch.object(self.moc, "run_command", return_value=completed) as run, patch.object(
            self.moc, "emit"
        ):
            exit_code = self.moc.run_wrapper(args)
        self.assertEqual(exit_code, 0)
        self.assertEqual(run.call_args.args[0][0], str(self.moc.POWERSHELL))

    def test_matscript_success_requires_ok_log_in_project_files_directory(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT / "tmp") as directory:
            scratch = Path(directory)

            def fake_run(command, cwd, **kwargs):
                (cwd / "import_xsd_test.pl.out").write_text("Imported: model\n", encoding="ascii")
                log_dir = cwd / "import_xsd_test_Files"
                log_dir.mkdir()
                (log_dir / "MatStudioLog.htm").write_text(
                    "Completion status: (OK)", encoding="ascii"
                )
                return subprocess.CompletedProcess(command, 0, "runner output", "")

            with patch.object(self.moc, "TEMP_ROOT", scratch), patch.object(
                self.moc, "run_command", side_effect=fake_run
            ):
                audit = self.moc.run_matscript("import_xsd_test", ["input_000.xsd"])
            self.assertTrue(audit["success"])
            self.assertTrue(audit["matstudio_log"].endswith("import_xsd_test_Files\\MatStudioLog.htm"))

    def test_matscript_success_requires_every_expected_output(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT / "tmp") as directory:
            scratch = Path(directory)

            def fake_run(command, cwd, **kwargs):
                log_dir = cwd / "import_xsd_test_Files"
                log_dir.mkdir()
                (log_dir / "MatStudioLog.htm").write_text(
                    "Completion status: (OK)", encoding="ascii"
                )
                (cwd / "model.car").write_bytes(b"car")
                return subprocess.CompletedProcess(command, 0, "runner output", "")

            with patch.object(self.moc, "TEMP_ROOT", scratch), patch.object(
                self.moc, "run_command", side_effect=fake_run
            ):
                audit = self.moc.run_matscript(
                    "import_xsd_test", [], expected_outputs=["model.car", "model.mdf"]
                )
            self.assertFalse(audit["success"])
            self.assertFalse(audit["outputs_complete"])
            self.assertEqual([item["exists"] for item in audit["generated_outputs"]], [True, False])

    def test_matscript_rejects_expected_output_traversal_before_staging(self) -> None:
        with patch.object(self.moc, "prepare_temp") as prepare:
            with self.assertRaisesRegex(ValueError, "safe relative path"):
                self.moc.run_matscript("import_xsd_test", [], expected_outputs=["../escape.car"])
        prepare.assert_not_called()

    def test_g01_report_validation_is_hash_bound_and_scope_limited(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT / "tmp") as directory:
            project = Path(directory)
            reports = project / "reports"
            reports.mkdir()
            report = reports / "G01_V1_REPRODUCTION_REPORT.json"
            gates = {
                "structure": "pass",
                "forcefield": "pass",
                "lammps_preflight": "pass",
                "scientific_validation": "pass",
                "science_contract": "pass",
            }
            report.write_text(json.dumps({
                "status": "pass",
                "project_status": "validated",
                "fresh_run": True,
                "project_validation_status": "valid",
                "production_science_released": False,
                "conversion": {"production_released": False},
                "energy_equivalence": {
                    "pass": True,
                    "absolute_difference_kcal_mol": 0.00003,
                    "tolerance_kcal_mol": 0.001,
                },
                "quality_gates": gates,
            }), encoding="utf-8")
            digest = hashlib.sha256(report.read_bytes()).hexdigest()
            (project / "manifest.json").write_text(json.dumps({
                "project": {"status": "validated"},
                "quality_gates": gates,
                "artifacts": [{
                    "path": "reports/G01_V1_REPRODUCTION_REPORT.json",
                    "sha256": digest,
                }],
            }), encoding="utf-8")
            result = self.moc.validate_g01_report(report)
            self.assertEqual(result["status"], "pass")
            self.assertFalse(result["production_science_released"])

            report.write_text(report.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            tampered = self.moc.validate_g01_report(report)
            self.assertEqual(tampered["status"], "fail")
            self.assertTrue(any("missing or changed" in item for item in tampered["errors"]))

    def test_science_status_separates_platform_acceptance_from_model_release(self) -> None:
        result = self.moc.collect_science_status()
        self.assertEqual(result["status"], "audited", result)
        self.assertFalse(result["production_science_released"])
        self.assertFalse(result["qualification_suite_released"])
        self.assertTrue(result["release_readiness"]["platform_acceptance_is_separate"])
        self.assertGreater(result["release_readiness"]["blocker_count"], 0)
        self.assertFalse(result["release_readiness"]["requires_user_action"])
        blocker_codes = {item["code"] for item in result["release_readiness"]["blockers"]}
        self.assertIn("G06_CONSTRUCTION_CONTRACT_INCOMPLETE", blocker_codes)
        target = result["target_reproduction"]
        self.assertFalse(target["production_released"])
        self.assertIn(target["status"], {"released", "blocked"})
        self.assertEqual(target["exact_contract_status"], "draft_blocked_missing_authoritative_inputs")
        self.assertTrue(target["exact_contract_sources_valid"])
        self.assertTrue(target["exact_contract_artifacts_valid"])
        self.assertIn("CH03_EXACT_REPRODUCTION_CONTRACT_NOT_FROZEN", blocker_codes)
        self.assertIn("CH03_PAPER_GEOMETRY_DEFINITION_UNRESOLVED", blocker_codes)
        self.assertIn("CH03_MINERAL_FORCEFIELD_NAMD_MAPPING_REQUIRED", blocker_codes)
        self.assertEqual(target["paper_mineral_formula"], "Si8Al4O20(OH)4")
        self.assertIn("pyrophyllite-like", target["mineral_identity_warning"])
        self.assertEqual(
            target["geometry_consistency_audit"]["arithmetic_replication_xy_A"],
            [157.2, 164.52],
        )
        self.assertEqual(
            target["geometry_consistency_audit"]["stated_projected_xy_A"],
            [154.61, 160.89],
        )
        self.assertEqual(target["mineral_parameter_candidate"]["nonbond_form"], "PCFF 9-6")
        if target["clay_is_surrogate"]:
            self.assertIn("CH03_PAPER_EQUIVALENT_MINERAL_LAYER_REQUIRED", blocker_codes)
        if target["missing_input_count"]:
            self.assertIn("CH03_PRODUCTION_INPUT_FILES_MISSING", blocker_codes)
        self.assertTrue(target["required_inputs"]["ch4_ua.pdb"])
        self.assertTrue(target["required_inputs"]["co2_trappe_rigid.pdb"])
        self.assertTrue(target["refprop_installation_detected"])
        self.assertTrue(target["refprop_available"])
        self.assertEqual(target["refprop_validation"]["status"], "pass")
        self.assertEqual(target["refprop_validation"]["refprop_version"], "10.0")
        self.assertNotIn("CH03_REFPROP_OR_DECLARED_ALTERNATIVE_REQUIRED", blocker_codes)
        self.assertEqual(set(result["models"]), {"G02", "G04", "G06"})
        self.assertFalse(result["models"]["G02"]["production_released"])
        self.assertEqual(
            result["models"]["G02"]["qualification_released"],
            result["models"]["G02"]["status"] == "cutoff_sensitivity_pass",
        )
        self.assertEqual(result["models"]["G02"]["role"], "qualification_fixture")
        self.assertEqual(result["models"]["G02"]["analysis_lock"]["status"], "pass")
        self.assertEqual(
            result["models"]["G02"]["analysis_lock"]["provenance_strength"],
            "local_filesystem_timestamp_not_third_party_attested",
        )
        self.assertEqual(result["models"]["G02"]["execution_adjudication_lock"]["status"], "pass")
        self.assertEqual(result["models"]["G02"]["execution_adjudication_lock"]["gate_id"], "G02-CS-02B")
        if result["models"]["G02"]["status"] in {"cutoff_sensitivity_pass", "cutoff_sensitivity_fail"}:
            self.assertTrue(result["models"]["G02"]["terminal_evidence_chain_valid"])
        self.assertTrue(result["models"]["G04"]["fixed_box_pressure_warning"])
        self.assertFalse(result["models"]["G04"]["z_only_relaxation_allowed"])
        self.assertTrue(result["models"]["G04"]["gates"]["pressure_tensor_diagnostic"]["hash_matches"])
        self.assertTrue(result["models"]["G04"]["thermodynamic_gate"]["registration_hash_matches"])
        self.assertEqual(result["models"]["G04"]["thermodynamic_gate"]["analysis_lock"]["status"], "pass")
        self.assertIn(
            result["models"]["G04"]["thermodynamic_gate"]["status"],
            {
                "triaxial_thermodynamic_gate_running",
                "triaxial_thermodynamic_gate_resume_required",
                "triaxial_thermodynamic_gate_complete_pending_analysis",
                "triaxial_thermodynamic_gate_execution_failed",
                "thermodynamic_equilibration_pass",
                "thermodynamic_equilibration_fail",
            },
        )
        if result["models"]["G04"]["thermodynamic_gate"]["status"].startswith("thermodynamic_equilibration_"):
            self.assertTrue(result["models"]["G04"]["thermodynamic_gate"]["terminal_evidence_chain_valid"])
        if result["models"]["G04"]["thermodynamic_gate"]["status"] == "thermodynamic_equilibration_pass":
            self.assertTrue(result["models"]["G04"]["qualification_released"])
            self.assertNotIn("G04_PRODUCTION_OBSERVABLE_PROTOCOL_REQUIRED", blocker_codes)
        self.assertEqual(result["models"]["G06"]["blocker_count"], 38)
        self.assertEqual(result["models"]["G06"]["verified_literature_sources"], 6)
        self.assertEqual(result["models"]["G06"]["verified_visual_reviews"], 18)
        self.assertTrue(result["models"]["G06"]["contract_hash_matches"])
        self.assertFalse(result["models"]["G06"]["authentication_required"])
        self.assertEqual(
            result["models"]["G06"]["authenticated_methods_status"],
            "accepted_as_hash_bound_main_method_evidence",
        )
        self.assertEqual(
            result["models"]["G06"]["authenticated_methods_sha256"],
            "47457747D8EF476BFA126CCE0D0091E731AB0B9BFFE92A289D7AFD8C3F9C7E82",
        )
        self.assertTrue(result["models"]["G06"]["authenticated_methods_hash_matches"])
        self.assertNotIn("G06_AUTHENTICATED_METHODS_REQUIRED", blocker_codes)

    def test_ch03_production_release_requires_a_complete_hash_bound_evidence_chain(self) -> None:
        required_inputs = (
            "graphene_mmt_pore_3nm.pdb",
            "graphene_mmt_pore_3nm.psf",
            "co2_trappe_rigid.pdb",
            "ch4_ua.pdb",
            "forcefield_ch03.prm",
        )
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT / "tmp") as directory:
            root = Path(directory)
            contract = root / "ch03_exact_reproduction_contract.json"
            contract.write_text('{"contract_id":"CH03-EXACT-01"}', encoding="utf-8")
            input_hashes = {}
            for name in required_inputs:
                path = root / name
                path.write_text(name, encoding="ascii")
                input_hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest().upper()
            analysis = root / "analyze_ch03_production.py"
            analysis.write_text("# frozen analyzer\n", encoding="ascii")
            artifacts = []
            for role in (
                "trajectory",
                "restart",
                "energy_log",
                "block_statistics",
                "region_definition",
                "provenance",
            ):
                path = root / f"{role}.dat"
                path.write_text(role, encoding="ascii")
                artifacts.append({
                    "role": role,
                    "path": path.name,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest().upper(),
                })
            evidence = {
                "schema_version": 1,
                "contract_id": "CH03-EXACT-01",
                "status": "production_pass",
                "production_released": True,
                "exact_contract_sha256": hashlib.sha256(contract.read_bytes()).hexdigest().upper(),
                "required_input_sha256": input_hashes,
                "engine": {"name": "NAMD", "version": "3.0.2", "executable_sha256": "A" * 64},
                "protocol": {
                    "equilibration_ns": 2.0,
                    "production_sampling_ns": 1.0,
                    "sampling_blocks": 5,
                    "timestep_fs": 1.0,
                    "temperature_K": 323.0,
                },
                "gates": {
                    "trajectory_complete": True,
                    "numerically_stable": True,
                    "fixed_walls_verified": True,
                    "region_definition_verified": True,
                    "five_block_statistics_complete": True,
                    "pressure_mapping_verified": True,
                    "provenance_complete": True,
                },
                "artifacts": artifacts,
                "analysis_implementation": {
                    "path": analysis.name,
                    "sha256": hashlib.sha256(analysis.read_bytes()).hexdigest().upper(),
                },
            }
            evidence_path = root / "ch03_production_evidence.json"
            evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
            valid = self.moc.validate_ch03_production_evidence(root, contract, required_inputs)
            self.assertEqual(valid["status"], "pass", valid)
            self.assertTrue(valid["production_released"])

            (root / "trajectory.dat").write_text("tampered", encoding="ascii")
            tampered = self.moc.validate_ch03_production_evidence(root, contract, required_inputs)
            self.assertEqual(tampered["status"], "fail")
            self.assertFalse(tampered["production_released"])

    def test_ch03_refprop10_evidence_is_hash_bound_and_tamper_evident(self) -> None:
        source_root = WORKSPACE_ROOT / "02_建模输入" / "ch03_竞争吸附"
        contract = json.loads(
            (source_root / "ch03_exact_reproduction_contract.json").read_text(encoding="utf-8")
        )
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT / "tmp") as directory:
            workspace = Path(directory)
            chapter = workspace / "02_建模输入" / "ch03_竞争吸附"
            chapter.mkdir(parents=True)
            for name in (
                "ch03_refprop10_preregistration.json",
                "ch03_pressure_mapping_refprop10.py",
                "ch03_refprop10_evidence.json",
            ):
                (chapter / name).write_bytes((source_root / name).read_bytes())

            valid = self.moc.validate_ch03_refprop10_evidence(workspace, chapter, contract)
            self.assertEqual(valid["status"], "pass", valid)
            self.assertEqual(valid["refprop_version"], "10.0")

            evidence = chapter / "ch03_refprop10_evidence.json"
            evidence.write_text(evidence.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            tampered = self.moc.validate_ch03_refprop10_evidence(workspace, chapter, contract)
            self.assertEqual(tampered["status"], "fail")
            self.assertTrue(any("evidence is missing or changed" in item for item in tampered["errors"]))

    def test_science_status_cli_emits_result_and_returns_success(self) -> None:
        science = {"status": "audited", "production_science_released": False}
        args = SimpleNamespace(science_root=None, json=True)
        with patch.object(self.moc, "collect_science_status", return_value=science), patch.object(
            self.moc, "emit"
        ) as emit:
            exit_code = self.moc.science_status(args)
        self.assertEqual(exit_code, 0)
        emit.assert_called_once_with(
            {"title": "MCP/MOC model science status", "result": science}, True
        )

    def test_deployed_moc_falls_back_to_known_science_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as deployment:
            with patch.object(self.moc, "ROOT", Path(deployment)):
                resolved = self.moc.resolve_science_root()
        self.assertEqual(resolved, (WORKSPACE_ROOT / "07_mcp_materials_studio").resolve())

    def test_g02_phase_classifier_does_not_hide_failed_or_completed_runs(self) -> None:
        classify = self.moc.classify_g02_phases
        self.assertEqual(classify({"a": "failed", "b": "pending"}), "cutoff_sensitivity_execution_failed")
        self.assertEqual(classify({"a": "failed", "b": "running"}), "cutoff_sensitivity_execution_failed")
        self.assertEqual(classify({"a": "resume_required", "b": "running"}), "cutoff_sensitivity_resume_required")
        self.assertEqual(classify({"a": "complete", "b": "pending"}), "cutoff_sensitivity_partially_complete")
        self.assertEqual(
            classify({"a": "complete", "b": "complete"}),
            "cutoff_sensitivity_complete_pending_analysis",
        )

    def test_g04_phase_classifier_surfaces_failures_before_running(self) -> None:
        classify = self.moc.classify_g04_th01_phases
        self.assertEqual(classify({"a": "failed", "b": "running"}), "triaxial_thermodynamic_gate_execution_failed")
        self.assertEqual(classify({"a": "resume_required", "b": "running"}), "triaxial_thermodynamic_gate_resume_required")

    def test_lammps_observed_progress_parser_ignores_non_thermo_numbers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "segment.log"
            header = "Step Time Temp Press Pxx Pyy Pzz Pxy Pxz Pyz Pe Ke Etotal Vol Lx Ly Lz Xy Xz Yz Atoms"
            row = "700 175 298 1 2 3 4 5 6 7 -100 10 -90 56000 41 36 37 0.1 -2 -2 5216"
            log.write_text(f"5216 atoms\n{header}\n{row}\n", encoding="utf-8")
            self.assertEqual(self.moc.max_lammps_thermo_step([log], field_count=21), 700)

    def test_acceptance_embeds_but_does_not_override_model_science_release(self) -> None:
        science = {"status": "audited", "production_science_released": False}
        doctor = {"status": "ready"}
        g01 = {"status": "pass"}
        regression = subprocess.CompletedProcess([], 0, "Ran 152 tests\nOK\n", "")
        dependencies = subprocess.CompletedProcess([], 0, "No broken requirements found.\n", "")
        with patch.object(self.moc, "collect_science_status", return_value=science), patch.object(
            self.moc, "collect_doctor", return_value=doctor
        ), patch.object(self.moc, "validate_g01_report", return_value=g01), patch.object(
            self.moc, "run_command", side_effect=[regression, dependencies]
        ):
            result = self.moc.collect_acceptance(Path("unused"))
        self.assertEqual(result["status"], "pass")
        self.assertIs(result["model_science"], science)
        self.assertFalse(result["production_science_released"])


class McpMocAdapterTests(unittest.TestCase):
    def test_status_adapter_uses_versioned_envelope(self) -> None:
        moc_status = {"schema_version": 2, "summary": {"status": "ready"}}
        with patch.object(server, "get_moc_status", return_value=moc_status):
            result = server.ms_moc_get_status()
        self.assertTrue(result["ok"])
        self.assertEqual(result["tool"], "ms_moc_get_status")
        self.assertEqual(result["data"], moc_status)

    def test_dry_run_validates_hash_and_replays_without_second_launch(self) -> None:
        with tempfile.TemporaryDirectory(prefix="moc_adapter_", dir=WORKSPACE_ROOT / "tmp") as directory:
            project = Path(
                initialize_project("moc-dry-run", "MOC dry run", projects_root=directory)["project_directory"]
            )
            document = project / "model" / "candidate.xsd"
            document.write_bytes(b"candidate model")
            digest = hashlib.sha256(document.read_bytes()).hexdigest().upper()
            moc_result = {
                "title": "MOC desktop launch",
                "result": {
                    "status": "dry_run",
                    "document": str(document),
                    "document_sha256": digest,
                },
            }
            with patch.object(server, "launch_document", return_value=moc_result) as launch:
                first = server.ms_moc_open_document(
                    str(project), str(document), digest, "moc-dry-run-key", dry_run=True
                )
                second = server.ms_moc_open_document(
                    str(project), str(document), digest, "moc-dry-run-key", dry_run=True
                )
            self.assertTrue(first["ok"])
            self.assertFalse(first["replayed"])
            self.assertTrue(second["ok"])
            self.assertTrue(second["replayed"])
            launch.assert_called_once_with(document.resolve(), dry_run=True)

    def test_hash_mismatch_fails_before_launch(self) -> None:
        with tempfile.TemporaryDirectory(prefix="moc_hash_", dir=WORKSPACE_ROOT / "tmp") as directory:
            project = Path(
                initialize_project("moc-hash", "MOC hash", projects_root=directory)["project_directory"]
            )
            document = project / "model" / "candidate.xsd"
            document.write_bytes(b"candidate model")
            with patch.object(server, "launch_document") as launch:
                result = server.ms_moc_open_document(
                    str(project), str(document), "0" * 64, "moc-hash-key", dry_run=True
                )
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], "invalid_request")
            launch.assert_not_called()

    def test_unsupported_suffix_fails_before_confirmation_or_launch(self) -> None:
        with tempfile.TemporaryDirectory(prefix="moc_suffix_", dir=WORKSPACE_ROOT / "tmp") as directory:
            project = Path(
                initialize_project("moc-suffix", "MOC suffix", projects_root=directory)["project_directory"]
            )
            document = project / "model" / "candidate.exe"
            document.write_bytes(b"not a model")
            digest = hashlib.sha256(document.read_bytes()).hexdigest().upper()
            with patch.object(server, "launch_document") as launch, patch.object(
                server.confirmation_manager, "consume"
            ) as consume:
                result = server.ms_moc_open_document(
                    str(project), str(document), digest, "moc-suffix-key", dry_run=False,
                    confirmation_token="unused",
                )
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], "invalid_request")
            consume.assert_not_called()
            launch.assert_not_called()

    def test_real_launch_requires_exact_confirmation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="moc_confirm_", dir=WORKSPACE_ROOT / "tmp") as directory:
            project = Path(
                initialize_project("moc-confirm", "MOC confirm", projects_root=directory)["project_directory"]
            )
            document = project / "model" / "candidate.xsd"
            document.write_bytes(b"candidate model")
            digest = hashlib.sha256(document.read_bytes()).hexdigest().upper()
            parameters = {
                "project_directory": str(project),
                "document_path": str(document),
                "document_sha256": digest,
                "idempotency_key": "moc-confirm-key",
                "dry_run": False,
            }
            missing = server.ms_moc_open_document(
                str(project), str(document), digest, "moc-missing-token", dry_run=False
            )
            self.assertFalse(missing["ok"])
            self.assertEqual(missing["error"]["code"], "permission_denied")

            confirmation = server.md_prepare_production_confirmation(
                "ms_moc_open_document", parameters
            )
            moc_result = {
                "title": "MOC desktop launch",
                "result": {
                    "status": "launched",
                    "document": str(document),
                    "document_sha256": digest,
                    "pid": 1234,
                },
            }
            with patch.object(server, "launch_document", return_value=moc_result) as launch:
                opened = server.ms_moc_open_document(
                    str(project), str(document), digest, "moc-confirm-key", dry_run=False,
                    confirmation_token=confirmation["confirmation_token"],
                )
            self.assertTrue(opened["ok"])
            self.assertEqual(opened["data"]["status"], "launched")
            launch.assert_called_once_with(document.resolve(), dry_run=False)

    def test_document_must_belong_to_bound_project(self) -> None:
        with tempfile.TemporaryDirectory(prefix="moc_project_", dir=WORKSPACE_ROOT / "tmp") as directory:
            root = Path(directory)
            project_a = Path(
                initialize_project("moc-a", "MOC A", projects_root=str(root / "a"))["project_directory"]
            )
            project_b = Path(
                initialize_project("moc-b", "MOC B", projects_root=str(root / "b"))["project_directory"]
            )
            document = project_b / "model" / "candidate.xsd"
            document.write_bytes(b"candidate model")
            digest = hashlib.sha256(document.read_bytes()).hexdigest().upper()
            result = server.ms_moc_open_document(
                str(project_a), str(document), digest, "moc-wrong-project", dry_run=True
            )
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], "permission_denied")


class MocMcpBridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.bridge = _load_bridge_module()

    def test_status_requires_tools_and_healthy_pipeline(self) -> None:
        names = list(self.bridge.REQUIRED_BRIDGE_TOOLS)
        ready = self.bridge.status_payload(
            names, {"status": "ready", "ready_for_ms_lammps_vmd": True}
        )
        self.assertEqual(ready["status"], "ready")
        self.assertTrue(ready["pipeline_ready"])

        degraded = self.bridge.status_payload(
            names, {"status": "degraded", "ready_for_ms_lammps_vmd": False}
        )
        self.assertEqual(degraded["status"], "degraded")
        self.assertFalse(degraded["pipeline_ready"])

        missing = self.bridge.status_payload(
            names[:-1], {"status": "ready", "ready_for_ms_lammps_vmd": True}
        )
        self.assertEqual(missing["status"], "degraded")
        self.assertFalse(missing["required_bridge_tools"][names[-1]])

    def test_failed_mcp_envelope_is_not_reported_as_completed(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "contract rejected"):
            self.bridge.require_successful_tool_result(
                "ms_geology_assess_nanopore_contract",
                {"ok": False, "error": {"message": "contract rejected"}},
            )

    def test_invalid_contract_data_is_not_reported_as_completed(self) -> None:
        with self.assertRaisesRegex(ValueError, "Missing top-level fields"):
            self.bridge.require_valid_nanopore_assessment({
                "ok": True,
                "data": {"status": "fail", "errors": ["Missing top-level fields"]},
            })

    def test_blocked_but_valid_contract_is_a_successful_assessment(self) -> None:
        result = {
            "ok": True,
            "data": {"status": "blocked", "errors": [], "construction_released": False},
        }
        self.assertIs(self.bridge.require_valid_nanopore_assessment(result), result)

    def test_exception_group_reports_the_root_cause(self) -> None:
        root = ValueError("specific contract failure")
        grouped = ExceptionGroup("stdio task failed", [ExceptionGroup("nested", [root])])
        self.assertIs(self.bridge.root_exception(grouped), root)


if __name__ == "__main__":
    unittest.main()
