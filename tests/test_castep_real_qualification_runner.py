from __future__ import annotations

from contextlib import ExitStack
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from materials_studio_mcp import castep_real_qualification_runner as p3b
from materials_studio_mcp.capability_registry import load_capability_registry


FROZEN_PLAN = Path(__file__).parents[1] / "docs" / "validation" / "receipts" / "p3c-corrected-real-castep-qualification-plan.json"
RETIRED_PLAN = Path(__file__).parents[1] / "docs" / "validation" / "receipts" / "p3a-real-castep-qualification-plan.json"
SAFE_BATCH_FIXTURE = Path(__file__).parent / "fixtures" / "p3b command" / "echo_args.cmd"
COMPLETED_OUTPUT = (
    "CASTEP test output\n"
    "Final energy = -12.345678 eV\n"
    "Total time = 1.5 s\n"
    "Calculation completed successfully\n"
)


class FakeProcess:
    next_pid = 424242
    output_text: str | None = COMPLETED_OUTPUT
    return_code = 0
    calls: list[tuple[str, dict]] = []

    def __init__(self, command: str, **kwargs: object) -> None:
        self.pid = self.next_pid
        self.returncode = self.return_code
        self.command = command
        self.kwargs = kwargs
        type(self).calls.append((command, kwargs))
        if self.output_text is not None:
            job = Path(str(kwargs["cwd"]))
            seed = command.rsplit(" ", 1)[-1].rstrip('"')
            (job / f"{seed}.castep").write_text(self.output_text, encoding="utf-8")

    def poll(self) -> int:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode


class RealCastepQualificationRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeProcess.calls = []
        FakeProcess.output_text = COMPLETED_OUTPUT
        FakeProcess.return_code = 0

    def _authorization(self, nonce: str = "A" * 64) -> dict:
        return p3b.create_single_use_authorization(
            plan_sha256=p3b.APPROVED_PLAN_SHA256,
            nonce=nonce,
        )

    def _plan(self, root: Path, seed: str = "quartz_alpha_sp_4c") -> dict:
        return {
            "plan_sha256": p3b.APPROVED_PLAN_SHA256,
            "input": {
                "seedname": seed,
                "hashes": {"manifest_sha256": "1" * 64},
            },
            "runtime": {
                "cores": 4,
                "hard_timeout_seconds": 600,
                "qualification_root": str(root),
                "launcher": {"sha256": "2" * 64},
                "command_interpreter": {"sha256": "3" * 64},
                "launcher_arguments": ["-np", "4", seed],
                "windows_raw_command_line": (
                    f'"{root / "cmd.exe"}" /d /s /c '
                    f'""{root / "RunCASTEP.bat"}" -np 4 {seed}"'
                ),
            },
        }

    def _execute(
        self,
        root: Path,
        *,
        authorization: dict | None = None,
        popen: object = FakeProcess,
        parser: object | None = None,
        parser_error: Exception | None = None,
        verify_side_effect: object | None = None,
    ) -> dict:
        root.mkdir(parents=True, exist_ok=True)
        manifest = root / "standalone_input_manifest.json"
        manifest.write_text("{}", encoding="utf-8")
        plan = self._plan(root)
        launcher = {"path": str(root / "RunCASTEP.bat"), "sha256": "2" * 64, "bytes": 1}
        command_interpreter = {"path": str(root / "cmd.exe"), "sha256": "3" * 64, "bytes": 1}
        copied = {
            "input_source_copy_sha256": "4" * 64,
            "cell_sha256": "5" * 64,
            "param_sha256": "6" * 64,
            "contract_file_sha256": "7" * 64,
        }

        def copy_inputs(_manifest: Path, job: Path, seed: str, _hashes: dict) -> dict:
            for name in ("input_source.xsd", f"{seed}.cell", f"{seed}.param", "standalone_input_contract.json"):
                (job / name).write_text(name, encoding="ascii")
            return copied.copy()

        completed = {
            "status": "completed",
            "classification": "completed",
            "energy": {"unit": "eV", "value": -12.345678},
        }
        with ExitStack() as stack:
            stack.enter_context(patch.object(p3b, "_QUALIFICATION_ROOT", root))
            stack.enter_context(patch.object(p3b, "_load_plan", return_value=plan))
            stack.enter_context(patch.object(
                p3b, "_fixed_file", side_effect=[launcher, command_interpreter]
            ))
            stack.enter_context(patch.object(
                p3b, "_validate_input_contract",
                return_value=(plan["input"]["seedname"], {"manifest_sha256": "1" * 64}, []),
            ))
            stack.enter_context(patch.object(p3b, "_copy_bound_inputs", side_effect=copy_inputs))
            if verify_side_effect is None:
                stack.enter_context(patch.object(
                    p3b, "_verify_staged_inputs", side_effect=lambda _job, expected: (expected.copy(), [])
                ))
            else:
                stack.enter_context(patch.object(p3b, "_verify_staged_inputs", side_effect=verify_side_effect))
            stack.enter_context(patch.object(p3b, "_snapshot_windows_processes", return_value={}))
            stack.enter_context(patch.object(p3b, "_pid_exists", return_value=False))
            stack.enter_context(patch.object(p3b.subprocess, "Popen", popen))
            if parser_error is None:
                stack.enter_context(patch.object(
                    p3b, "parse_standalone_castep_result",
                    return_value=completed if parser is None else parser,
                ))
            else:
                stack.enter_context(patch.object(
                    p3b, "parse_standalone_castep_result", side_effect=parser_error
                ))
            return p3b.execute_real_castep_qualification_once(
                plan_path=FROZEN_PLAN,
                input_manifest=manifest,
                authorization=authorization or self._authorization(),
            )

    def test_frozen_plan_is_exactly_the_approved_plan(self) -> None:
        plan = p3b._load_plan(FROZEN_PLAN)
        self.assertEqual(plan["plan_sha256"], p3b.APPROVED_PLAN_SHA256)
        self.assertEqual(plan["runtime"]["cores"], 4)
        self.assertEqual(plan["runtime"]["hard_timeout_seconds"], 600)
        self.assertFalse(plan["execution_allowed"])

    def test_consumed_r1_plan_is_permanently_retired(self) -> None:
        with self.assertRaisesRegex(ValueError, "permanently retired"):
            p3b._load_plan(RETIRED_PLAN)

    def test_raw_windows_command_line_handles_quoted_batch_path_without_backslash_quotes(self) -> None:
        command_interpreter = Path(os.environ["SystemRoot"]) / "System32" / "cmd.exe"
        raw_command_line = (
            f'"{command_interpreter}" /d /s /c '
            f'""{SAFE_BATCH_FIXTURE.resolve()}" -np 4 quartz_alpha_sp_4c"'
        )
        completed = subprocess.run(
            raw_command_line,
            executable=str(command_interpreter),
            cwd=Path(__file__).parent,
            shell=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn(
            "P3B_SAFE_FIXTURE_OK:-np 4 quartz_alpha_sp_4c", completed.stdout
        )
        self.assertNotIn(r'\"', raw_command_line)

    def test_authorization_is_canonical_hash_bound_and_tamper_fails(self) -> None:
        authorization = self._authorization("ab" * 32)
        self.assertEqual(authorization["nonce"], ("AB" * 32))
        payload = {key: value for key, value in authorization.items() if key != "authorization_sha256"}
        self.assertEqual(authorization["authorization_sha256"], p3b._canonical_sha256(payload))
        for key, value in (
            ("action", "other"), ("plan_sha256", "0" * 64), ("cores", 5),
            ("timeout_seconds", 601), ("max_executions", 2), ("nonce", "bad"),
        ):
            with self.subTest(key=key):
                altered = copy.deepcopy(authorization)
                altered[key] = value
                with self.assertRaises(ValueError):
                    p3b.validate_single_use_authorization(altered)
        unsigned = dict(payload)
        with self.assertRaises(ValueError):
            p3b.validate_single_use_authorization(unsigned)

    def test_plan_tamper_and_platform_hash_drift_fail_closed(self) -> None:
        plan = json.loads(FROZEN_PLAN.read_text(encoding="utf-8"))
        plan["runtime"]["cores"] = 5
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "plan.json"
            path.write_text(json.dumps(plan), encoding="utf-8")
            with self.assertRaises(ValueError):
                p3b._load_plan(path)
        with patch.object(p3b, "sha256_file", return_value="0" * 64):
            with self.assertRaises(PermissionError):
                p3b._fixed_file(Path(__file__), "F" * 64, "drifted file")

    def test_fixed_command_uses_no_shell_and_receipt_binds_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = self._execute(Path(temporary))
            receipt = json.loads(Path(result["receipt_path"]).read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "qualification_pass")
        self.assertEqual(len(FakeProcess.calls), 1)
        command, kwargs = FakeProcess.calls[0]
        expected = self._plan(Path(temporary))["runtime"]["windows_raw_command_line"]
        self.assertEqual(command, expected)
        self.assertEqual(kwargs["executable"], str(Path(temporary) / "cmd.exe"))
        self.assertIs(kwargs["shell"], False)
        self.assertEqual(kwargs["stdin"], p3b.subprocess.DEVNULL)
        self.assertEqual(receipt["authorization_sha256"], result["authorization_sha256"])
        self.assertEqual(receipt["staged_input_hashes_before_launch"], receipt["copied_input_hashes"])
        self.assertEqual(receipt["staged_input_hashes_after_exit"], receipt["copied_input_hashes"])
        self.assertTrue(receipt["logs"]["stdout_sha256"])
        self.assertTrue(any(item["name"].endswith(".castep") and item["sha256"] for item in receipt["artifacts"]))

    def test_authorization_replay_is_rejected_before_second_process(self) -> None:
        authorization = self._authorization("B" * 64)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = self._execute(root, authorization=authorization)
            second = self._execute(root, authorization=authorization)
        self.assertEqual(first["status"], "qualification_pass")
        self.assertEqual(second["status"], "runner_error")
        self.assertEqual(len(FakeProcess.calls), 1)
        self.assertIn("FileExistsError", second["errors"][-1]["detail"])

    def test_start_failure_is_receipted_and_consumes_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = self._execute(Path(temporary), popen=OSError("mock start failure"))
            receipt = json.loads(Path(result["receipt_path"]).read_text(encoding="utf-8"))
            marker_exists = Path(result["authorization_consumption_marker"]).is_file()
        self.assertEqual(result["status"], "runner_error")
        self.assertEqual(receipt["errors"][0]["code"], "RUNNER_ERROR")
        self.assertTrue(marker_exists)

    def test_staged_input_drift_blocks_before_authorization_and_process(self) -> None:
        calls = 0

        def drift(_job: Path, expected: dict) -> tuple[dict, list[dict[str, str]]]:
            nonlocal calls
            calls += 1
            return expected.copy(), [{"code": "STAGED_INPUT_HASH_MISMATCH", "detail": "mock drift"}]

        with tempfile.TemporaryDirectory() as temporary:
            result = self._execute(Path(temporary), verify_side_effect=drift)
        self.assertEqual(result["status"], "staged_input_changed_before_launch")
        self.assertEqual(calls, 1)
        self.assertEqual(FakeProcess.calls, [])
        self.assertNotIn("authorization_consumption_marker", result)

    def test_parser_failure_never_qualifies(self) -> None:
        parser = {"status": "failed", "classification": "fatal_error", "energy": None}
        with tempfile.TemporaryDirectory() as temporary:
            result = self._execute(Path(temporary), parser=parser)
        self.assertEqual(result["status"], "output_parse_failed")

    def test_parser_exception_is_receipted_as_postprocess_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = self._execute(
                Path(temporary), parser_error=RuntimeError("mock parser exception")
            )
            receipt = json.loads(Path(result["receipt_path"]).read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "postprocess_error")
        self.assertEqual(receipt["errors"][-1]["code"], "POSTPROCESS_ERROR")

    def test_lock_failure_does_not_consume_authorization_or_start_process(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            p3b, "acquire_execution_slot", side_effect=RuntimeError("mock slot busy")
        ):
            result = self._execute(Path(temporary))
        self.assertEqual(result["status"], "runner_error")
        self.assertNotIn("authorization_consumption_marker", result)
        self.assertEqual(FakeProcess.calls, [])

    def test_tree_cleanup_exception_records_fallback_and_fails_closed(self) -> None:
        process = Mock(pid=424243)
        receipt = {"process": {"termination": None}, "errors": []}
        fallback = {"pid": process.pid, "method": "root_fallback", "root_terminated": True}
        with patch.object(
            p3b, "_terminate_process_tree", side_effect=RuntimeError("mock tree failure")
        ), patch.object(p3b, "_tree_termination_fallback", return_value=fallback):
            p3b._terminate_owned_process(process, receipt)
        self.assertEqual(receipt["process"]["termination"], fallback)
        self.assertEqual(receipt["errors"][0]["code"], "PROCESS_TREE_TERMINATION_FAILED")

    def test_nonzero_and_missing_output_never_qualify(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            FakeProcess.return_code = 7
            nonzero = self._execute(Path(temporary) / "nonzero")
            FakeProcess.return_code = 0
            FakeProcess.output_text = None
            missing = self._execute(Path(temporary) / "missing", authorization=self._authorization("C" * 64))
        self.assertEqual(nonzero["status"], "nonzero_exit")
        self.assertEqual(missing["status"], "output_missing")

    def test_registry_does_not_release_public_castep(self) -> None:
        capabilities = {item["id"]: item for item in load_capability_registry()["capabilities"]}
        private = capabilities["castep.real_qualification_execution_candidate"]
        self.assertEqual(private["status"], "todo")
        self.assertFalse(private["verified"])
        self.assertEqual(private["exposure"], "not_implemented")
        public_run = capabilities["castep.calculation"]
        public_parse = capabilities["results.castep_parsing"]
        self.assertFalse(public_run["verified"])
        self.assertEqual(public_run["exposure"], "not_implemented")
        self.assertFalse(public_parse["verified"])
        self.assertEqual(public_parse["exposure"], "not_implemented")


if __name__ == "__main__":
    unittest.main()
