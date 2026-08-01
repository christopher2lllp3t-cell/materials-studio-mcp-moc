from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from materials_studio_mcp.pipeline_config import (
    acquire_execution_slot,
    approved_executable,
    bounded_mpi_processes,
    bounded_timeout,
    load_pipeline_config,
    resolve_output_path,
    resolve_workspace_path,
)
from materials_studio_mcp.structure_preflight import inspect_msi2lmp_inputs


ROOT = Path(r"D:\分子动力学模拟")


class ExecutionPolicyTests(unittest.TestCase):
    def test_unicode_workspace_root_is_preserved_and_exists(self) -> None:
        policy = load_pipeline_config()["policy"]
        roots = [Path(path) for path in policy["workspace_roots"]]
        self.assertIn(ROOT, roots)
        self.assertTrue(ROOT.is_dir())

    def test_path_traversal_outside_roots_is_rejected(self) -> None:
        with self.assertRaises(PermissionError):
            resolve_workspace_path(Path(tempfile.gettempdir()) / "outside.data")

    def test_configured_scratch_root_is_allowed(self) -> None:
        scratch = load_pipeline_config()["policy"]["scratch_root"]
        self.assertEqual(resolve_workspace_path(scratch), Path(scratch).resolve())

    def test_existing_output_is_rejected(self) -> None:
        target = ROOT / "tmp" / "policy_existing.data"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("owned", encoding="utf-8")
        try:
            with self.assertRaises(FileExistsError):
                resolve_output_path(target)
        finally:
            target.unlink(missing_ok=True)

    def test_timeout_and_mpi_limits_are_enforced(self) -> None:
        self.assertEqual(bounded_timeout(None), 120)
        with self.assertRaises(ValueError):
            bounded_timeout(3601)
        self.assertEqual(bounded_mpi_processes(8), 8)
        with self.assertRaises(ValueError):
            bounded_mpi_processes(9)

    def test_executable_requires_exact_approved_path(self) -> None:
        with self.assertRaises(PermissionError):
            approved_executable(r"C:\Windows\System32\cmd.exe")

    def test_policy_declares_single_job_and_fixed_executables(self) -> None:
        policy = load_pipeline_config()["policy"]
        self.assertEqual(policy["limits"]["max_parallel_jobs"], 1)
        paths = policy["allowed_executable_paths"]
        self.assertTrue(paths)
        self.assertTrue(all(Path(item).is_absolute() for item in paths))

    def test_cross_process_lock_file_blocks_second_slot(self) -> None:
        with acquire_execution_slot():
            with self.assertRaises(RuntimeError):
                with acquire_execution_slot():
                    pass
        with acquire_execution_slot() as lock_path:
            self.assertTrue(lock_path.is_file())

    def test_forcefield_cannot_escape_library(self) -> None:
        car = ROOT / "tmp" / "policy.car"
        mdf = ROOT / "tmp" / "policy.mdf"
        car.write_text("", encoding="utf-8")
        mdf.write_text("", encoding="utf-8")
        try:
            result = inspect_msi2lmp_inputs(str(car), str(mdf), str(ROOT / "not-approved.frc"))
            self.assertEqual(result["status"], "blocked")
            self.assertTrue(any("frc_files" in error for error in result["errors"]))
        finally:
            car.unlink(missing_ok=True)
            mdf.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
