from __future__ import annotations

import shutil
import subprocess
import tempfile
import re
import json
import os
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .pipeline_config import (acquire_execution_slot, approved_executable, bounded_timeout, load_pipeline_config,
                              resolve_output_path, resolve_workspace_path)
from .structure_preflight import inspect_lammps_data, inspect_msi2lmp_inputs


def convert_car_mdf(car_path: str, mdf_path: str, output_data_path: str, forcefield_file: str,
                    forcefield_class: str = "I", timeout_seconds: int = 300) -> dict[str, Any]:
    config = load_pipeline_config()
    with acquire_execution_slot(config=config):
        return _convert_car_mdf_guarded(car_path, mdf_path, output_data_path, forcefield_file,
                                        forcefield_class, timeout_seconds, config)


def _terminate_process_tree(process: subprocess.Popen[str]) -> dict[str, Any]:
    """Terminate only the tree rooted at the process created for this job."""
    if process.poll() is not None:
        return {"requested": False, "pid": process.pid, "reason": "already_exited"}
    if os.name == "nt":
        taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
        result = subprocess.run([str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                errors="replace", timeout=15, check=False, close_fds=True)
        return {"requested": True, "pid": process.pid, "method": "taskkill_tree",
                "exit_code": result.returncode, "stdout": result.stdout[-2000:], "stderr": result.stderr[-2000:]}
    process.kill()
    process.wait(timeout=15)
    return {"requested": True, "pid": process.pid, "method": "kill_process_group"}


def _write_audit(job_dir: Path, metadata: dict[str, Any]) -> None:
    target = job_dir / "audit.json"
    with target.open("x", encoding="utf-8") as handle:
        json.dump(metadata, handle, ensure_ascii=False, indent=2)


def _convert_car_mdf_guarded(car_path: str, mdf_path: str, output_data_path: str,
                             forcefield_file: str, forcefield_class: str,
                             timeout_seconds: int, config: dict[str, Any]) -> dict[str, Any]:
    car = resolve_workspace_path(car_path, must_exist=True, config=config)
    mdf = resolve_workspace_path(mdf_path, must_exist=True, config=config)
    destination = resolve_output_path(output_data_path, config=config)
    timeout = bounded_timeout(timeout_seconds, config=config)
    preflight = inspect_msi2lmp_inputs(str(car), str(mdf), forcefield_file, forcefield_class)
    if preflight["status"] != "pass":
        return {"success": False, "stage": "input_preflight", "preflight": preflight}
    software = config["software"]["lammps"]
    executable = approved_executable(software["msi2lmp"], config=config)
    destination.parent.mkdir(parents=True, exist_ok=True)
    scratch = resolve_workspace_path(config["policy"]["scratch_root"], config=config)
    scratch.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="msi2lmp_", dir=scratch))
    retain_failure = bool(config["policy"].get("execution", {}).get("retain_failed_job_directories", True))
    started = datetime.now(timezone.utc)
    success = False
    try:
        root = work / "model"
        shutil.copy2(car, root.with_suffix(".car"))
        shutil.copy2(mdf, root.with_suffix(".mdf"))
        command = [str(executable), "model", "-print", "2", "-class", forcefield_class,
                   "-frc", preflight["forcefield_file"]]
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        process = subprocess.Popen(
            command, cwd=work, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, errors="replace",
            creationflags=creationflags, close_fds=True,
        )
        termination = None
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            termination = _terminate_process_tree(process)
            stdout, stderr = process.communicate(timeout=15)
            metadata = {"schema_version": 1, "status": "timeout", "pid": process.pid,
                        "started_utc": started.isoformat(), "ended_utc": datetime.now(timezone.utc).isoformat(),
                        "timeout_seconds": timeout, "command": command, "termination": termination,
                        "stdout_sha256": hashlib.sha256(stdout.encode()).hexdigest(),
                        "stderr_sha256": hashlib.sha256(stderr.encode()).hexdigest()}
            _write_audit(work, metadata)
            return {"success": False, "stage": "timeout", "job_directory": str(work),
                    "termination": termination, "stdout": stdout[-8000:], "stderr": stderr[-8000:]}
        completed = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        generated = work / "model.data"
        if completed.returncode != 0 or not generated.is_file():
            combined = completed.stdout + "\n" + completed.stderr
            missing_masses = sorted(set(re.findall(r"Unable to find mass for\s+(\S+)", combined)))
            inconsistent_connects = len(re.findall(r"WARNING inconsistent # of connects", combined))
            return {"success": False, "stage": "msi2lmp", "command": command, "exit_code": completed.returncode,
                    "job_directory": str(work),
                    "diagnostics": {"missing_mass_types": missing_masses,
                                    "inconsistent_connectivity_warning_count": inconsistent_connects,
                                    "recommendation": "Do not use -ignore. Select a compatible .frc or build a reviewed type/parameter mapping."},
                    "stdout": completed.stdout[-8000:], "stderr": completed.stderr[-8000:], "preflight": preflight}
        data_check = inspect_lammps_data(str(generated))
        if data_check["status"] != "pass":
            return {"success": False, "stage": "data_preflight", "data_preflight": data_check,
                    "job_directory": str(work),
                    "stdout": completed.stdout[-8000:], "stderr": completed.stderr[-8000:]}
        # Exclusive creation closes the check/copy race and can never overwrite.
        with generated.open("rb") as source, destination.open("xb") as target:
            shutil.copyfileobj(source, target)
        success = True
        return {"success": True, "stage": "complete", "output_data_path": str(destination),
            "input_preflight": preflight, "data_preflight": inspect_lammps_data(str(destination)),
            "stdout": completed.stdout[-8000:], "stderr": completed.stderr[-8000:]}
    finally:
        if not success and retain_failure and not (work / "audit.json").exists():
            stdout_value = completed.stdout if "completed" in locals() else ""
            stderr_value = completed.stderr if "completed" in locals() else ""
            _write_audit(work, {"schema_version": 1, "status": "failed",
                                "started_utc": started.isoformat(),
                                "ended_utc": datetime.now(timezone.utc).isoformat(),
                                "pid": process.pid if "process" in locals() else None,
                                "exit_code": completed.returncode if "completed" in locals() else None,
                                "timeout_seconds": timeout,
                                "command": command if "command" in locals() else None,
                                "output_path": str(destination),
                                "stdout_sha256": hashlib.sha256(stdout_value.encode()).hexdigest(),
                                "stderr_sha256": hashlib.sha256(stderr_value.encode()).hexdigest(),
                                "preserved_files": sorted(item.name for item in work.iterdir())})
        if success or not retain_failure:
            shutil.rmtree(work, ignore_errors=True)
