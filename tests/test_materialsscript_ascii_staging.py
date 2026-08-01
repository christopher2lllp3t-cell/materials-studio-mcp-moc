from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from materials_studio_mcp import server


class MaterialsScriptAsciiStagingTests(unittest.TestCase):
    def setUp(self) -> None:
        # These tests isolate ASCII staging mechanics. Execution-policy behavior
        # is covered separately with the real pipeline_config validators.
        patchers = [
            patch.object(server, "load_pipeline_config", return_value={"policy": {}}),
            patch.object(server, "approved_executable", side_effect=lambda path, **_: Path(path)),
            patch.object(server, "bounded_timeout", side_effect=lambda value, **_: value),
            patch.object(
                server, "resolve_workspace_path",
                side_effect=lambda path, **_: Path(path).resolve(strict=False),
            ),
            patch.object(
                server, "resolve_output_path",
                side_effect=lambda path, **_: Path(path).resolve(strict=False),
            ),
            patch.object(server, "acquire_execution_slot", side_effect=lambda **_: nullcontext()),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _ascii_workspace(self) -> tempfile.TemporaryDirectory[str]:
        return tempfile.TemporaryDirectory(prefix="ms_ascii_test_")

    @staticmethod
    def _fake_ms_paths(root: Path) -> dict[str, str]:
        bat = root / "etc" / "Scripting" / "bin" / "RunMatScript.bat"
        bat.parent.mkdir(parents=True, exist_ok=True)
        bat.write_text("@exit /b 0\n", encoding="ascii")
        return {"root": str(root), "run_mat_script": str(bat)}

    def test_unicode_source_and_destination_are_kept_outside_matserver_job(self) -> None:
        with self._ascii_workspace() as base_text:
            base = Path(base_text)
            self.assertTrue(str(base).isascii())
            ms_root = base / "ms_root"
            ms_paths = self._fake_ms_paths(ms_root)
            source = base / "external" / "中文输入" / "模型.xsd"
            source.parent.mkdir(parents=True)
            source.write_text("<XSD />", encoding="utf-8")
            destination = base / "external" / "中文输出" / "结果.xsd"
            scratch = base / "ascii_scratch"
            observed: dict[str, object] = {}

            def fake_run(
                command: list[str], *, cwd: Path, timeout_seconds: int
            ) -> tuple[subprocess.CompletedProcess[str], bool, None, int]:
                observed["cwd"] = cwd
                self.assertTrue(str(cwd).isascii())
                job_name = command[-1]
                script_path = cwd / f"{job_name}.pl"
                script = script_path.read_text(encoding="utf-8")
                observed["script"] = script
                self.assertNotIn(str(source), script)
                self.assertNotIn(str(destination), script)
                (cwd / "outputs" / "result.xsd").write_text("result", encoding="ascii")
                (cwd / f"{job_name}.pl.out").write_text("", encoding="utf-8")
                (cwd / f"{job_name}MatStudioLog.htm").write_text(
                    "Completion status: (OK). Exiting MatServer: status OK.", encoding="utf-8"
                )
                return subprocess.CompletedProcess(command, 0, "", ""), False, None, 1234

            with patch.dict(
                "os.environ", {server.MATERIALSSCRIPT_SCRATCH_ENV: str(scratch)}
            ), patch.object(server, "_materials_studio_paths", return_value=ms_paths), patch.object(
                server, "_run_guarded_materialsscript_process", side_effect=fake_run
            ):
                result = server._run_materialsscript_job(
                    script_template=(
                        'my $doc = Documents->Import("{{input.structure}}");\n'
                        '$doc->Export("{{output.result}}");\n'
                    ),
                    input_files={"structure": str(source)},
                    output_files={
                        "result": {
                            "relative_path": "result.xsd",
                            "destination_path": str(destination),
                        }
                    },
                    job_name="中文作业",
                    run_mode="flat",
                    keep_job_dir=True,
                    timeout_seconds=30,
                )

            self.assertTrue(result["success"])
            self.assertTrue(destination.exists())
            self.assertTrue(str(observed["cwd"]).isascii())
            self.assertTrue(Path(result["staged_inputs"]["structure"]["staged_path"]).is_file())
            self.assertTrue(result["staged_inputs"]["structure"]["staged_path"].isascii())

    def test_unicode_absolute_path_literal_is_rejected_before_execution(self) -> None:
        with self._ascii_workspace() as base_text:
            base = Path(base_text)
            ms_paths = self._fake_ms_paths(base / "ms_root")
            with patch.dict(
                "os.environ", {server.MATERIALSSCRIPT_SCRATCH_ENV: str(base / "scratch")}
            ), patch.object(server, "_materials_studio_paths", return_value=ms_paths), patch.object(
                server.subprocess, "run"
            ) as run:
                with self.assertRaisesRegex(ValueError, "non-ASCII absolute path literal"):
                    server._run_materialsscript_job(
                        script_template='Documents->Import("D:/分子/模型.xsd");',
                        input_files=None,
                        output_files=None,
                        job_name="unsafe",
                        run_mode="flat",
                        keep_job_dir=False,
                        timeout_seconds=30,
                    )
                run.assert_not_called()

    def test_output_path_cannot_escape_ascii_job_directory(self) -> None:
        with self._ascii_workspace() as base_text:
            base = Path(base_text)
            ms_paths = self._fake_ms_paths(base / "ms_root")
            with patch.dict(
                "os.environ", {server.MATERIALSSCRIPT_SCRATCH_ENV: str(base / "scratch")}
            ), patch.object(server, "_materials_studio_paths", return_value=ms_paths), patch.object(
                server.subprocess, "run"
            ) as run:
                with self.assertRaisesRegex(ValueError, "escapes the MaterialsScript job directory"):
                    server._run_materialsscript_job(
                        script_template='open(my $fh, ">", "{{output.bad}}");',
                        input_files=None,
                        output_files={"bad": {"relative_path": "../../../escape.txt"}},
                        job_name="unsafe_output",
                        run_mode="flat",
                        keep_job_dir=False,
                        timeout_seconds=30,
                    )
                run.assert_not_called()

    def test_helper_process_also_starts_from_ascii_scratch(self) -> None:
        with self._ascii_workspace() as base_text:
            scratch = Path(base_text) / "scratch"

            def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                self.assertTrue(str(kwargs["cwd"]).isascii())
                input_path = Path(command[command.index("-InputFile") + 1])
                self.assertTrue(str(input_path).isascii())
                return subprocess.CompletedProcess(
                    command, 0, json.dumps({"ok": True, "data": {"checked": True}}), ""
                )

            with patch.dict(
                "os.environ", {server.MATERIALSSCRIPT_SCRATCH_ENV: str(scratch)}
            ), patch.object(server.subprocess, "run", side_effect=fake_run):
                result = server._run_helper("detect")
            self.assertEqual(result["data"], {"checked": True})

    def test_explicit_unicode_scratch_root_fails_closed(self) -> None:
        with self._ascii_workspace() as base_text:
            unsafe = Path(base_text) / "中文临时目录"
            with patch.dict(
                "os.environ", {server.MATERIALSSCRIPT_SCRATCH_ENV: str(unsafe)}
            ):
                with self.assertRaisesRegex(ValueError, "ASCII characters only"):
                    server._materialsscript_scratch_root()

    def test_car_mdf_export_does_not_override_failed_matstudio_log(self) -> None:
        with self._ascii_workspace() as base_text:
            base = Path(base_text)
            input_xsd = base / "input.xsd"
            input_xsd.write_text("<XSD />", encoding="ascii")
            output_car = base / "output.car"
            output_mdf = base / "output.mdf"
            failed_result = {
                "success": False,
                "run_mat_script_exit_code": 0,
                "outputs": {
                    "car": {"exists": True},
                    "mdf": {"exists": True},
                },
                "error_summary": "Completion status: (FAIL)",
            }
            with patch.object(server, "_run_materialsscript_job", return_value=failed_result):
                result = server.md_export_xsd_to_car_mdf(
                    str(input_xsd), str(output_car), str(output_mdf), 30
                )
            self.assertFalse(result["success"])
            self.assertTrue(result["outputs_untrusted"])
            self.assertIn("FAIL", result["error_summary"])

    def test_project_mode_car_export_reads_project_log_and_accepts_pair(self) -> None:
        with self._ascii_workspace() as base_text:
            base = Path(base_text)
            ms_paths = self._fake_ms_paths(base / "ms_root")
            input_xsd = base / "input.xsd"
            input_xsd.write_text("<XSD />", encoding="ascii")
            output_car = base / "accepted.car"
            output_mdf = base / "accepted.mdf"

            def fake_run(
                command: list[str], *, cwd: Path, timeout_seconds: int
            ) -> tuple[subprocess.CompletedProcess[str], bool, None, int]:
                job_name = command[-1]
                (cwd / "outputs" / "model.car").write_text("!BIOSYM archive 3\n", encoding="ascii")
                (cwd / "outputs" / "model.mdf").write_text("!BIOSYM molecular_data 4\n", encoding="ascii")
                project_files = cwd / f"{job_name}_Files"
                project_files.mkdir()
                (project_files / "MatStudioLog.htm").write_text(
                    "Completion status: (OK). Exiting MatServer: status OK.", encoding="utf-8"
                )
                (cwd / f"{job_name}.pl.out").write_text("", encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "", ""), False, None, 4321

            with patch.dict(
                "os.environ", {server.MATERIALSSCRIPT_SCRATCH_ENV: str(base / "scratch")}
            ), patch.object(server, "_materials_studio_paths", return_value=ms_paths), patch.object(
                server, "_run_guarded_materialsscript_process", side_effect=fake_run
            ):
                result = server.md_export_xsd_to_car_mdf(
                    str(input_xsd), str(output_car), str(output_mdf), 30
                )

            self.assertTrue(result["success"])
            self.assertTrue(output_car.is_file())
            self.assertTrue(output_mdf.is_file())
            self.assertIn("MatStudioLog", result["success_basis"])

    def test_failed_matstudio_job_never_publishes_staged_output(self) -> None:
        with self._ascii_workspace() as base_text:
            base = Path(base_text)
            ms_paths = self._fake_ms_paths(base / "ms_root")
            source = base / "input.xsd"
            source.write_text("<XSD />", encoding="ascii")
            destination = base / "must_not_exist.xsd"

            def fake_run(
                command: list[str], *, cwd: Path, timeout_seconds: int
            ) -> tuple[subprocess.CompletedProcess[str], bool, None, int]:
                job_name = command[-1]
                (cwd / "outputs" / "result.xsd").write_text("untrusted", encoding="ascii")
                (cwd / f"{job_name}MatStudioLog.htm").write_text(
                    "Completion status: (FAIL). Exiting MatServer: status OK.", encoding="utf-8"
                )
                return subprocess.CompletedProcess(command, 0, "", ""), False, None, 999

            with patch.dict(
                "os.environ", {server.MATERIALSSCRIPT_SCRATCH_ENV: str(base / "scratch")}
            ), patch.object(server, "_materials_studio_paths", return_value=ms_paths), patch.object(
                server, "_run_guarded_materialsscript_process", side_effect=fake_run
            ):
                result = server._run_materialsscript_job(
                    script_template=(
                        'my $doc = Documents->Import("{{input.structure}}");\n'
                        '$doc->Export("{{output.result}}");\n'
                    ),
                    input_files={"structure": str(source)},
                    output_files={"result": {"relative_path": "result.xsd", "destination_path": str(destination)}},
                    job_name="failed_publish",
                    run_mode="flat",
                    keep_job_dir=True,
                    timeout_seconds=30,
                )

            self.assertFalse(result["success"])
            self.assertFalse(destination.exists())
            self.assertIsNone(result["outputs"]["result"]["copied_to"])

    def test_invalid_mode_and_unresolved_placeholder_fail_before_execution(self) -> None:
        with self._ascii_workspace() as base_text:
            base = Path(base_text)
            ms_paths = self._fake_ms_paths(base / "ms_root")
            with patch.dict(
                "os.environ", {server.MATERIALSSCRIPT_SCRATCH_ENV: str(base / "scratch")}
            ), patch.object(server, "_materials_studio_paths", return_value=ms_paths), patch.object(
                server, "_run_guarded_materialsscript_process"
            ) as run:
                with self.assertRaisesRegex(ValueError, "run_mode"):
                    server._run_materialsscript_job(
                        script_template="print 1;", input_files=None, output_files=None,
                        job_name="bad_mode", run_mode="unsafe", keep_job_dir=True, timeout_seconds=30,
                    )
                with self.assertRaisesRegex(ValueError, "unresolved template placeholder"):
                    server._run_materialsscript_job(
                        script_template='print "{{missing.value}}";', input_files=None, output_files=None,
                        job_name="bad_placeholder", run_mode="flat", keep_job_dir=True, timeout_seconds=30,
                    )
                run.assert_not_called()

    def test_guarded_process_terminates_only_owned_tree_on_timeout(self) -> None:
        class FakeProcess:
            pid = 24680
            returncode = 1

            def __init__(self) -> None:
                self.calls = 0

            def communicate(self, timeout: int):
                self.calls += 1
                if self.calls == 1:
                    raise subprocess.TimeoutExpired(["runner"], timeout)
                return "partial stdout", "timeout stderr"

        fake = FakeProcess()
        termination = {"requested": True, "pid": fake.pid, "method": "taskkill_tree"}
        with patch.object(server.subprocess, "Popen", return_value=fake) as popen, patch.object(
            server, "_terminate_process_tree", return_value=termination
        ) as terminate:
            completed, timed_out, observed, pid = server._run_guarded_materialsscript_process(
                ["runner"], cwd=Path.cwd(), timeout_seconds=5
            )
        self.assertTrue(timed_out)
        self.assertEqual(pid, fake.pid)
        self.assertEqual(observed, termination)
        self.assertEqual(completed.stdout, "partial stdout")
        self.assertIs(popen.call_args.kwargs["stdin"], subprocess.DEVNULL)
        terminate.assert_called_once_with(fake)


if __name__ == "__main__":
    unittest.main()
