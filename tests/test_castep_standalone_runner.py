from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from materials_studio_mcp import castep_standalone_runner as runner
from materials_studio_mcp.capability_registry import load_capability_registry
from materials_studio_mcp.castep_standalone import prepare_castep_standalone_inputs
from materials_studio_mcp.pipeline_config import _pid_exists, acquire_execution_slot


def write_periodic_xsd(path: Path) -> str:
    path.write_text(
        "<XSD Version='23.1'>"
        '<Atom3d ID="1" Components="Si" XYZ="0,0,0" />'
        '<Atom3d ID="2" Components="O" XYZ="0.25,0.25,0.25" />'
        '<Atom3d ID="3" ImageOf="1" />'
        '<SpaceGroup ITNumber="225" GroupName="FM-3M" '
        'Operators="1,0,0,0,0,1,0,0,0,0,1,0:1,0,0,0.5,0,1,0,0.5,0,0,1,0:'
        '1,0,0,0,0,1,0,0.5,0,0,1,0.5:1,0,0,0.5,0,1,0,0,0,0,1,0.5" '
        'AVector="5,0,0" BVector="0,5,0" CVector="0,0,5" />'
        "</XSD>",
        encoding="utf-8",
    )
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def standalone_context(digest: str) -> dict:
    return {
        "schema_version": 1, "task": "single_point", "purpose": "preliminary", "input_sha256": digest,
        "electronic_character": {"value": "insulator", "source": "P2 test"},
        "magnetism": {"value": "nonmagnetic", "source": "P2 test"},
        "dispersion": {"value": "off", "source": "P2 test"},
        "pseudopotentials": {"value": "default_otfg", "source": "P2 test"},
        "xc_functional": {"value": "PBE", "source": "P2 test"},
        "energy_cutoff_ev": {"value": 600.0, "source": "P2 test"},
        "kpoint_mp_grid": {"value": [3, 3, 3], "source": "P2 test"},
        "convergence_evidence": [],
    }


class CastepStandaloneRunnerTests(unittest.TestCase):
    def _candidate(self, root: Path) -> tuple[dict, Path]:
        root.mkdir(parents=True, exist_ok=True)
        source = root / "source.xsd"
        digest = write_periodic_xsd(source)
        candidate = prepare_castep_standalone_inputs(
            input_xsd=source, input_sha256=digest, output_directory=root / "candidate",
            calculation_name="p2 runner", standalone_context=standalone_context(digest), dry_run=False,
        )
        return candidate, Path(candidate["manifest"]["path"])

    def _run(self, root: Path, scenario: str = "normal", **kwargs: object) -> tuple[dict, dict]:
        candidate, manifest = self._candidate(root)
        result = runner.run_synthetic_standalone_qualification(
            input_manifest=manifest, job_root=root / "jobs", scenario=scenario, **kwargs
        )
        receipt = json.loads(Path(result["receipt_path"]).read_text(encoding="utf-8"))
        return result, receipt

    def test_real_entry_is_unconditionally_blocked_without_popen(self) -> None:
        with patch("materials_studio_mcp.castep_standalone_runner.subprocess.Popen") as popen:
            result = runner.run_standalone_castep(
                input_manifest=Path("ignored.json"), job_root=Path("ignored"), cores=4, timeout_seconds=1
            )
        self.assertEqual(result["status"], "blocked_real_castep_execution")
        self.assertFalse(result["executed"])
        popen.assert_not_called()

    def test_normal_synthetic_run_qualifies_process_control_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result, receipt = self._run(Path(temporary), timeout_seconds=5)
            job = Path(result["job"]["directory"])
            self.assertTrue((job / "runner.stdout.log").is_file())
            self.assertTrue((job / "runner.stderr.log").is_file())
        self.assertEqual(result["status"], "qualified_process_control")
        self.assertTrue(result["synthetic"])
        self.assertTrue(result["process_control_qualified"])
        self.assertEqual(result["parser"]["status"], "completed")
        self.assertEqual(result["scientific_release"]["castep_execution"], "unverified")
        self.assertEqual(receipt["adapter"]["helper_sha256"], runner._FAKE_HELPER_SHA256)
        self.assertTrue(receipt["logs"]["stdout_sha256"])
        self.assertEqual(receipt["staged_input_hashes_before_launch"], receipt["copied_input_hashes"])
        self.assertEqual(receipt["staged_input_hashes_after_run"], receipt["copied_input_hashes"])

    def test_start_failure_is_receipted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch(
            "materials_studio_mcp.castep_standalone_runner.subprocess.Popen", side_effect=OSError("synthetic start failure")
        ):
            result, receipt = self._run(Path(temporary), timeout_seconds=5)
            stdout_exists = Path(result["process"]["stdout_path"]).is_file()
        self.assertEqual(result["status"], "start_failed")
        self.assertEqual(receipt["errors"][0]["code"], "PROCESS_START_FAILED")
        self.assertTrue(stdout_exists)

    def test_timeout_terminates_the_owned_synthetic_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result, receipt = self._run(Path(temporary), "sleep", timeout_seconds=1)
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(receipt["process"]["termination"]["pid"], receipt["process"]["pid"])
        self.assertEqual(receipt["process"]["termination"]["method"], "taskkill_tree")

    def test_timeout_terminates_parent_child_and_grandchild_without_orphans(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result, _ = self._run(Path(temporary), "tree", timeout_seconds=2)
            pid_file = Path(result["job"]["directory"]) / "synthetic_tree_pids.txt"
            pids = [int(line.rsplit(":", 1)[1]) for line in pid_file.read_text(encoding="ascii").splitlines()]
            for _ in range(20):
                if all(not _pid_exists(pid) for pid in pids):
                    break
                time.sleep(0.1)
        self.assertEqual(result["status"], "timeout")
        self.assertGreaterEqual(len(pids), 3)
        self.assertTrue(all(not _pid_exists(pid) for pid in pids))

    def test_explicit_cancellation_terminates_the_owned_process(self) -> None:
        event = threading.Event()
        timer = threading.Timer(0.25, event.set)
        with tempfile.TemporaryDirectory() as temporary:
            timer.start()
            try:
                result, receipt = self._run(Path(temporary), "sleep", timeout_seconds=10, cancel_event=event)
            finally:
                timer.cancel()
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(receipt["process"]["termination"]["pid"], receipt["process"]["pid"])

    def test_nonzero_missing_and_unparseable_output_are_distinct(self) -> None:
        expected = {"nonzero": "nonzero_exit", "missing_output": "output_missing", "truncated": "output_parse_failed"}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for scenario, status in expected.items():
                with self.subTest(scenario=scenario):
                    result, _ = self._run(root / scenario, scenario, timeout_seconds=5)
                    self.assertEqual(result["status"], status)

    def test_cross_process_execution_lock_blocks_second_runner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, acquire_execution_slot():
            result, receipt = self._run(Path(temporary), timeout_seconds=5)
        self.assertEqual(result["status"], "blocked_lock")
        self.assertEqual(receipt["errors"][0]["code"], "EXECUTION_SLOT_UNAVAILABLE")

    def test_new_job_directory_never_overwrites_a_collision(self) -> None:
        token = "a" * 20
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate, manifest = self._candidate(root)
            collision = root / "jobs" / f"p2_{candidate['seedname']}_{token}"
            collision.mkdir(parents=True)
            sentinel = collision / "sentinel.txt"
            sentinel.write_text("preserve", encoding="ascii")
            with patch("materials_studio_mcp.castep_standalone_runner.secrets.token_hex", return_value=token):
                result = runner.run_synthetic_standalone_qualification(
                    input_manifest=manifest, job_root=root / "jobs", timeout_seconds=5
                )
            sentinel_value = sentinel.read_text(encoding="ascii")
        self.assertEqual(result["status"], "job_directory_collision")
        self.assertEqual(sentinel_value, "preserve")

    def test_input_change_after_staging_blocks_before_process_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate, manifest = self._candidate(root)
            original = runner._copy_bound_inputs

            def copy_then_mutate(*args: object, **kwargs: object) -> dict[str, str]:
                copied = original(*args, **kwargs)
                Path(candidate["param"]["path"]).write_text("changed-after-copy\n", encoding="ascii")
                return copied

            with patch("materials_studio_mcp.castep_standalone_runner._copy_bound_inputs", side_effect=copy_then_mutate), patch(
                "materials_studio_mcp.castep_standalone_runner.subprocess.Popen"
            ) as popen:
                result = runner.run_synthetic_standalone_qualification(
                    input_manifest=manifest, job_root=root / "jobs", timeout_seconds=5
                )
        self.assertEqual(result["status"], "input_changed_before_launch")
        popen.assert_not_called()

    def test_staged_input_change_blocks_before_process_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, manifest = self._candidate(root)
            original = runner._copy_bound_inputs

            def copy_then_mutate_staged(*args: object, **kwargs: object) -> dict[str, str]:
                copied = original(*args, **kwargs)
                job_directory, seedname = Path(args[1]), str(args[2])
                (job_directory / f"{seedname}.param").write_text("tampered-staged-copy\n", encoding="ascii")
                return copied

            with patch("materials_studio_mcp.castep_standalone_runner._copy_bound_inputs", side_effect=copy_then_mutate_staged), patch(
                "materials_studio_mcp.castep_standalone_runner.subprocess.Popen"
            ) as popen:
                result = runner.run_synthetic_standalone_qualification(
                    input_manifest=manifest, job_root=root / "jobs", timeout_seconds=5
                )
        self.assertEqual(result["status"], "staged_input_changed_before_launch")
        self.assertEqual(result["errors"][0]["code"], "STAGED_INPUT_HASH_MISMATCH")
        popen.assert_not_called()

    def test_staged_input_change_during_run_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate, manifest = self._candidate(root)
            target = root / "jobs"
            changed = threading.Event()

            def mutate_when_staged() -> None:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    directories = list(target.glob("p2_*")) if target.is_dir() else []
                    if directories:
                        staged = directories[0] / f"{candidate['seedname']}.param"
                        started = directories[0] / "synthetic_tree_pids.txt"
                        if staged.is_file() and started.is_file():
                            staged.write_text("tampered-during-run\n", encoding="ascii")
                            changed.set()
                            return
                    time.sleep(0.02)

            mutator = threading.Thread(target=mutate_when_staged)
            mutator.start()
            result = runner.run_synthetic_standalone_qualification(
                input_manifest=manifest, job_root=target, scenario="write_then_sleep", timeout_seconds=5
            )
            mutator.join(timeout=5)
        self.assertTrue(changed.is_set())
        self.assertEqual(result["status"], "staged_input_changed_during_run")
        self.assertEqual(result["errors"][0]["code"], "STAGED_INPUT_HASH_MISMATCH")
        self.assertNotIn("process_control_qualified", result)

    def test_parser_runtime_error_is_not_reported_as_lock_contention(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch(
            "materials_studio_mcp.castep_standalone_runner.parse_standalone_castep_result", side_effect=RuntimeError("synthetic parser fault")
        ):
            result, receipt = self._run(Path(temporary), timeout_seconds=5)
            pid_file = Path(result["job"]["directory"]) / "synthetic_tree_pids.txt"
            pid = int(pid_file.read_text(encoding="ascii").split(":", 1)[1])
        self.assertEqual(result["status"], "internal_runner_error")
        self.assertEqual(receipt["errors"][0]["code"], "INTERNAL_RUNNER_ERROR")
        self.assertNotEqual(result["status"], "blocked_lock")
        for _ in range(20):
            if not _pid_exists(pid):
                break
            time.sleep(0.1)
        self.assertFalse(_pid_exists(pid))

    def test_tree_termination_runtime_error_fails_closed_and_kills_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch(
            "materials_studio_mcp.castep_standalone_runner._terminate_process_tree", side_effect=RuntimeError("synthetic tree cleanup fault")
        ):
            result, receipt = self._run(Path(temporary), "sleep", timeout_seconds=1)
            pid_file = Path(result["job"]["directory"]) / "synthetic_tree_pids.txt"
            pid = int(pid_file.read_text(encoding="ascii").split(":", 1)[1])
        self.assertEqual(result["status"], "process_cleanup_failed")
        self.assertEqual(receipt["errors"][0]["code"], "PROCESS_TREE_TERMINATION_FAILED")
        self.assertEqual(receipt["process"]["termination"]["method"], "tree_termination_failed_root_fallback")
        self.assertNotEqual(result["status"], "blocked_lock")
        self.assertFalse(_pid_exists(pid))

    def test_unicode_complete_job_path_is_blocked_before_creation_and_popen(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, manifest = self._candidate(root)
            unicode_root = root / "P2_非ASCII"
            with patch("materials_studio_mcp.castep_standalone_runner.subprocess.Popen") as popen:
                result = runner.run_synthetic_standalone_qualification(
                    input_manifest=manifest, job_root=unicode_root, timeout_seconds=5
                )
            unicode_root_created = unicode_root.exists()
        self.assertEqual(result["status"], "job_directory_not_ascii")
        self.assertEqual(result["errors"][0]["code"], "JOB_DIRECTORY_NOT_ASCII")
        self.assertFalse(unicode_root_created)
        popen.assert_not_called()

    def test_fixed_adapter_hash_mismatch_blocks_without_process_start(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with patch("materials_studio_mcp.castep_standalone_runner._FAKE_HELPER_SHA256", "F" * 64), patch(
                "materials_studio_mcp.castep_standalone_runner.subprocess.Popen"
            ) as popen:
                result, receipt = self._run(Path(temporary), timeout_seconds=5)
        self.assertEqual(result["status"], "synthetic_adapter_unavailable")
        self.assertEqual(receipt["errors"][0]["code"], "SYNTHETIC_ADAPTER_UNAVAILABLE")
        popen.assert_not_called()

    def test_runner_qualification_registry_is_private_and_public_states_are_unchanged(self) -> None:
        capabilities = {item["id"]: item for item in load_capability_registry()["capabilities"]}
        candidate = capabilities["castep.standalone_runner_process_control_qualification"]
        self.assertEqual(candidate["status"], "todo")
        self.assertFalse(candidate["verified"])
        self.assertEqual(candidate["exposure"], "not_implemented")
        self.assertEqual(capabilities["castep.calculation"]["status"], "unverified")
        self.assertEqual(capabilities["results.castep_parsing"]["status"], "unverified")


if __name__ == "__main__":
    unittest.main()
