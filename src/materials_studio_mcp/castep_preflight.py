from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

from .castep_pl import inspect_xsd


PREFLIGHT_ENVIRONMENT_VARIABLE = "MS_CASTEP_PL_PREFLIGHT_ONLY"
FORBIDDEN_CASTEP_SUFFIXES = frozenset(
    {".bands", ".castep", ".cell", ".check", ".geom", ".md", ".param", ".usp"}
)
FORBIDDEN_RESULT_NAMES = frozenset({"opt.xsd", "report.txt"})


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def _require_sha256(value: str, label: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9A-Fa-f]{64}", value) is None:
        raise ValueError(f"{label} must contain exactly 64 hexadecimal characters")
    return value.upper()


def _task_file(task_directory: Path, name: str, suffix: str, label: str) -> Path:
    if not isinstance(name, str) or Path(name).name != name or not name.lower().endswith(suffix):
        raise ValueError(f"{label} must be a basename ending in {suffix}")
    path = (task_directory / name).resolve()
    if path.parent != task_directory or not path.is_file():
        raise FileNotFoundError(f"{label} is missing from the task directory: {name}")
    return path


def _forbidden_artifacts(task_directory: Path) -> list[str]:
    return sorted(
        str(path)
        for path in task_directory.rglob("*")
        if path.is_file()
        and (path.name.lower() in FORBIDDEN_RESULT_NAMES or path.suffix.lower() in FORBIDDEN_CASTEP_SUFFIXES)
    )


def inspect_castep_preflight_plan(
    package_directory: Path,
    package_manifest_sha256: str,
    task_name: str,
) -> dict[str, Any]:
    package_directory = package_directory.resolve()
    if not package_directory.is_dir():
        raise FileNotFoundError(f"CASTEP package directory not found: {package_directory}")
    manifest_path = package_directory / "package_manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"CASTEP package manifest not found: {manifest_path}")
    expected_manifest_hash = _require_sha256(package_manifest_sha256, "package_manifest_sha256")
    actual_manifest_hash = sha256_file(manifest_path)
    if actual_manifest_hash != expected_manifest_hash:
        raise ValueError(
            f"CASTEP package manifest SHA-256 mismatch: expected {expected_manifest_hash}, got {actual_manifest_hash}"
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    allow_local = manifest.get("allow_local", False)
    if not isinstance(allow_local, bool):
        raise ValueError("CASTEP package manifest allow_local must be a boolean")
    tasks = manifest.get("tasks")
    if not isinstance(tasks, list):
        raise ValueError("CASTEP package manifest tasks must be an array")
    matches = [item for item in tasks if isinstance(item, dict) and item.get("task") == task_name]
    if len(matches) != 1:
        raise ValueError("task_name must identify exactly one manifest task")
    task = matches[0]
    directory_name = task.get("directory_name") or Path(str(task.get("directory", ""))).name
    if not isinstance(directory_name, str) or Path(directory_name).name != directory_name:
        raise ValueError("Manifest task directory name is invalid")
    task_directory = (package_directory / directory_name).resolve()
    if task_directory.parent != package_directory or not task_directory.is_dir():
        raise ValueError("Manifest task directory escapes or is missing from the package")

    pl_path = _task_file(task_directory, task.get("pl_document"), ".pl", "pl_document")
    xsd_path = _task_file(task_directory, task.get("xsd_document"), ".xsd", "xsd_document")
    expected_pl_hash = _require_sha256(task.get("pl_sha256"), "task.pl_sha256")
    expected_xsd_hash = _require_sha256(task.get("xsd_sha256"), "task.xsd_sha256")
    if sha256_file(pl_path) != expected_pl_hash:
        raise ValueError("Generated PL SHA-256 does not match the package manifest")
    if sha256_file(xsd_path) != expected_xsd_hash:
        raise ValueError("Copied XSD SHA-256 does not match the package manifest")
    expected_atoms = task.get("expected_atoms")
    if isinstance(expected_atoms, bool) or not isinstance(expected_atoms, int) or expected_atoms < 1:
        raise ValueError("Manifest expected_atoms must be a positive integer")
    xsd_metadata = inspect_xsd(xsd_path, expected_xsd_hash)
    if xsd_metadata["runtime_atom_count"] != expected_atoms:
        raise ValueError("Manifest expected_atoms does not match the inspected XSD runtime count")

    script = pl_path.read_text(encoding="utf-8")
    required_fragments = (
        f'my $model = "{xsd_path.name}";',
        f"my $expected_atoms = {expected_atoms};",
        "Documents->Import($model)",
        PREFLIGHT_ENVIRONMENT_VARIABLE,
        "RESULT status=preflight_only",
        "GeometryOptimization->Run",
    )
    if not allow_local:
        required_fragments += ("LOCAL EXECUTION BLOCKED",)
    missing = [fragment for fragment in required_fragments if fragment not in script]
    if missing:
        raise ValueError(f"Generated PL is missing required guarded fragments: {missing}")
    if "$Documents->Import($model)" in script:
        raise ValueError("Generated PL contains the invalid scalar Documents import form")
    preflight_index = script.index("RESULT status=preflight_only")
    castep_index = script.index("GeometryOptimization->Run")
    if preflight_index >= castep_index:
        raise ValueError("Generated PL preflight does not exit before CASTEP")
    local_guard = "LOCAL EXECUTION BLOCKED"
    if not allow_local and not preflight_index < script.index(local_guard) < castep_index:
        raise ValueError("Generated PL preflight does not exit before local execution and CASTEP")

    stdout_path = Path(f"{pl_path}.out")
    log_path = task_directory / f"{pl_path.stem}MatStudioLog.htm"
    receipt_path = task_directory / "castep_preflight_receipt.json"
    collisions = [str(path) for path in (stdout_path, log_path, receipt_path) if path.exists()]
    if collisions:
        raise FileExistsError(f"Refusing to overwrite existing CASTEP preflight evidence: {collisions}")
    forbidden = _forbidden_artifacts(task_directory)
    if forbidden:
        raise ValueError(f"Task directory already contains CASTEP/result artifacts: {forbidden}")
    return {
        "status": "validated",
        "package_directory": str(package_directory),
        "manifest_path": str(manifest_path),
        "manifest_sha256": actual_manifest_hash,
        "task": task_name,
        "task_directory": str(task_directory),
        "pl_path": str(pl_path),
        "pl_sha256": expected_pl_hash,
        "xsd_path": str(xsd_path),
        "xsd_sha256": expected_xsd_hash,
        "expected_atoms": expected_atoms,
        "allow_local": allow_local,
        "xsd_metadata": xsd_metadata,
        "stdout_path": str(stdout_path),
        "matstudio_log_path": str(log_path),
        "receipt_path": str(receipt_path),
        "automatic_submission": False,
        "gateway_selected": False,
        "castep_execution_allowed": False,
    }


def finalize_castep_preflight(
    plan: dict[str, Any],
    completed: subprocess.CompletedProcess[str],
    *,
    timed_out: bool,
    termination: dict[str, Any] | None,
    process_pid: int,
) -> dict[str, Any]:
    stdout_path = Path(plan["stdout_path"])
    log_path = Path(plan["matstudio_log_path"])
    task_directory = Path(plan["task_directory"])
    script_stdout = stdout_path.read_text(encoding="utf-8", errors="replace") if stdout_path.is_file() else ""
    log_html = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""
    expected_marker = f"RESULT status=preflight_only calculation={plan['task']}"
    expected_atoms_marker = f"atoms={plan['expected_atoms']}"
    checks = {
        "not_timed_out": not timed_out,
        "process_exit_zero": completed.returncode == 0,
        "result_marker": expected_marker in script_stdout,
        "runtime_atom_count": expected_atoms_marker in script_stdout,
        "matserver_completion_ok": "Completion status: (OK)" in log_html,
        "matserver_exit_ok": "Exiting MatServer: status OK" in log_html,
        "no_castep_or_result_artifacts": not _forbidden_artifacts(task_directory),
    }
    success = all(checks.values())
    receipt = {
        "schema_version": 1,
        "status": "preflight_pass" if success else "preflight_fail",
        "task": plan["task"],
        "automatic_submission": False,
        "gateway_selected": False,
        "castep_execution_started": False,
        "expected_atoms": plan["expected_atoms"],
        "manifest_sha256": plan["manifest_sha256"],
        "pl_sha256": plan["pl_sha256"],
        "xsd_sha256": plan["xsd_sha256"],
        "process": {
            "pid": process_pid,
            "exit_code": completed.returncode,
            "timed_out": timed_out,
            "termination": termination,
        },
        "checks": checks,
        "evidence": {
            "stdout_path": str(stdout_path) if stdout_path.is_file() else None,
            "stdout_sha256": sha256_file(stdout_path) if stdout_path.is_file() else None,
            "matstudio_log_path": str(log_path) if log_path.is_file() else None,
            "matstudio_log_sha256": sha256_file(log_path) if log_path.is_file() else None,
        },
    }
    receipt_path = Path(plan["receipt_path"])
    with receipt_path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(receipt, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return {
        **receipt,
        "receipt_path": str(receipt_path),
        "receipt_sha256": sha256_file(receipt_path),
        "shell_stdout": completed.stdout,
        "shell_stderr": completed.stderr,
    }
