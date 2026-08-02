from __future__ import annotations

"""Private P2 process-control qualification for standalone CASTEP inputs.

The real CASTEP entry below is permanently blocked in this candidate.  The only
spawn path is a fixed, hash-bound synthetic helper used by local regression
tests; it is not exposed through ``server.py`` and cannot accept a shell string
or an arbitrary executable.
"""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from typing import Any

from .castep_result_parser import _empty_result, _input_contract, parse_standalone_castep_result
from .conversion_executor import _terminate_process_tree
from .geology_modeling import sha256_file
from .pipeline_config import PROJECT_ROOT, acquire_execution_slot, bounded_timeout


RUNNER_SCHEMA_VERSION = 1
RUNNER_REVISION = "ms-mcp.private-castep-standalone-runner.1.3.0-p2-r1"
_SAFE_SEED = re.compile(r"^[A-Za-z0-9_]{1,48}$")
_FAKE_HELPER = PROJECT_ROOT / "tests" / "fixtures" / "castep_runner" / "synthetic_castep_helper.py"
# Updated only by a source-reviewed P2 change; do not derive this expected value
# from a caller-provided path or from an environment variable.
_FAKE_HELPER_SHA256 = "850247944B5F80EEA2DE6638C5F3E8855D7CCC32F72281126FEA720382AF172E"
_SYNTHETIC_SCENARIOS = frozenset({"normal", "sleep", "tree", "nonzero", "missing_output", "truncated"})


def _issue(code: str, detail: str) -> dict[str, str]:
    return {"code": code, "detail": detail}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _base_receipt(
    *,
    input_hashes: dict[str, Any],
    seedname: str | None,
    cores: int | None,
    timeout_seconds: int | None,
    status: str,
) -> dict[str, Any]:
    return {
        "schema_version": RUNNER_SCHEMA_VERSION,
        "runner_revision": RUNNER_REVISION,
        "mode": "synthetic_test_adapter",
        "status": status,
        "synthetic": True,
        "scientific_release": {
            "production_science_released": False,
            "castep_execution": "unverified",
            "castep_result_parsing": "unverified",
        },
        "blockers": [
            _issue("REAL_CASTEP_EXECUTION_BLOCKED", "P2 does not authorize RunCASTEP, CASTEP, Materials Studio, MPI, or Gateway execution."),
            _issue("SYNTHETIC_PROCESS_CONTROL_ONLY", "Synthetic helper success qualifies process control only, never CASTEP execution."),
        ],
        "input_hashes": input_hashes,
        "seedname": seedname,
        "resource_policy": {"cores": cores, "allowed_core_range": [1, 4], "timeout_seconds": timeout_seconds},
        "job": {"directory": None, "directory_name": None},
        "adapter": {"helper_path": None, "helper_sha256": None, "interpreter_path": None, "interpreter_sha256": None},
        "process": {
            "pid": None,
            "started_utc": None,
            "ended_utc": None,
            "exit_code": None,
            "termination": None,
            "stdin": "closed",
            "stdout_path": None,
            "stderr_path": None,
        },
        "logs": {"stdout_sha256": None, "stderr_sha256": None},
        "output": {"path": None, "sha256": None, "present": False},
        "parser": None,
        "errors": [],
    }


def _write_receipt(job_directory: Path, receipt: dict[str, Any]) -> Path:
    target = job_directory / "runner_receipt.json"
    with target.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(receipt, handle, ensure_ascii=True, indent=2, allow_nan=False)
        handle.write("\n")
    return target


def _finish(job_directory: Path | None, receipt: dict[str, Any]) -> dict[str, Any]:
    if job_directory is None:
        return receipt
    receipt_path = _write_receipt(job_directory, receipt)
    return {**receipt, "receipt_path": str(receipt_path)}


def _validate_cores(cores: int) -> None:
    if isinstance(cores, bool) or not isinstance(cores, int) or not 1 <= cores <= 4:
        raise ValueError("P2 standalone runner cores must be an integer from 1 to 4")


def _validate_input_contract(input_manifest: Path) -> tuple[str | None, dict[str, Any], list[dict[str, str]]]:
    parser_envelope = _empty_result(None, None, None)
    seedname, errors = _input_contract(input_manifest.resolve(), parser_envelope)
    return seedname, parser_envelope["input_hashes"], errors


def _create_job_directory(job_root: Path, seedname: str) -> Path:
    root = job_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    for _ in range(4):
        leaf = f"p2_{seedname}_{secrets.token_hex(10)}"
        if not leaf.isascii() or _SAFE_SEED.fullmatch(seedname) is None:
            raise ValueError("P2 standalone runner requires an ASCII-safe seedname")
        candidate = root / leaf
        try:
            candidate.mkdir(exist_ok=False)
            return candidate
        except FileExistsError:
            continue
    raise FileExistsError("P2 runner could not create a new non-overwriting ASCII job directory")


def _copy_bound_inputs(input_manifest: Path, job_directory: Path, seedname: str, input_hashes: dict[str, Any]) -> dict[str, str]:
    source_directory = input_manifest.resolve().parent
    files = {
        "manifest_sha256": (input_manifest.resolve(), "standalone_input_manifest.json"),
        "input_source_copy_sha256": (source_directory / "input_source.xsd", "input_source.xsd"),
        "cell_sha256": (source_directory / f"{seedname}.cell", f"{seedname}.cell"),
        "param_sha256": (source_directory / f"{seedname}.param", f"{seedname}.param"),
        "contract_file_sha256": (source_directory / "standalone_input_contract.json", "standalone_input_contract.json"),
    }
    copied: dict[str, str] = {}
    for hash_key, (source, destination_name) in files.items():
        if not source.is_file():
            raise FileNotFoundError(f"Bound standalone input is missing: {destination_name}")
        destination = job_directory / destination_name
        with source.open("rb") as reader, destination.open("xb") as writer:
            shutil.copyfileobj(reader, writer)
        actual = sha256_file(destination)
        expected = input_hashes.get(hash_key)
        if not isinstance(expected, str) or actual != expected.upper():
            raise RuntimeError(f"Bound standalone input changed while being copied: {destination_name}")
        copied[destination_name] = actual
    return copied


def _synthetic_adapter() -> tuple[Path, str, Path, str]:
    helper = _FAKE_HELPER.resolve()
    if not helper.is_file():
        raise FileNotFoundError("Fixed P2 synthetic helper is missing")
    helper_hash = sha256_file(helper)
    if helper_hash != _FAKE_HELPER_SHA256:
        raise PermissionError("Fixed P2 synthetic helper SHA-256 does not match its reviewed value")
    interpreter = Path(sys.executable).resolve()
    if not interpreter.is_file():
        raise FileNotFoundError("Current Python interpreter is missing")
    return helper, helper_hash, interpreter, sha256_file(interpreter)


def _input_changed(before: dict[str, Any], after: dict[str, Any], errors: list[dict[str, str]]) -> bool:
    return bool(errors) or any(before.get(key) != after.get(key) for key in before)


def run_standalone_castep(
    *,
    input_manifest: Path,
    job_root: Path,
    cores: int = 4,
    timeout_seconds: int | None = None,
) -> dict[str, Any]:
    """Unconditionally block the real runner; it never resolves or starts RunCASTEP."""

    _validate_cores(cores)
    return {
        "schema_version": RUNNER_SCHEMA_VERSION,
        "runner_revision": RUNNER_REVISION,
        "mode": "real_castep",
        "status": "blocked_real_castep_execution",
        "executed": False,
        "input_manifest_supplied": isinstance(input_manifest, Path),
        "job_root_supplied": isinstance(job_root, Path),
        "resource_policy": {"cores": cores, "timeout_seconds": timeout_seconds},
        "blockers": [_issue("REAL_CASTEP_EXECUTION_BLOCKED", "P2 is a no-CASTEP qualification stage and never invokes RunCASTEP.bat.")],
    }


def run_synthetic_standalone_qualification(
    *,
    input_manifest: Path,
    job_root: Path,
    scenario: str = "normal",
    cores: int = 4,
    timeout_seconds: int | None = None,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Run only the fixed synthetic helper for private P2 process-control tests."""

    _validate_cores(cores)
    if scenario not in _SYNTHETIC_SCENARIOS:
        raise ValueError("P2 synthetic scenario is not allowlisted")
    timeout = bounded_timeout(timeout_seconds)
    manifest = Path(input_manifest).resolve()
    seedname, input_hashes, input_errors = _validate_input_contract(manifest)
    if input_errors or seedname is None:
        receipt = _base_receipt(input_hashes=input_hashes, seedname=seedname, cores=cores, timeout_seconds=timeout, status="input_contract_invalid")
        receipt["errors"] = input_errors or [_issue("INPUT_CONTRACT_INVALID", "The standalone input contract could not be verified.")]
        return receipt
    try:
        job_directory = _create_job_directory(Path(job_root), seedname)
    except FileExistsError as exc:
        receipt = _base_receipt(input_hashes=input_hashes, seedname=seedname, cores=cores, timeout_seconds=timeout, status="job_directory_collision")
        receipt["errors"] = [_issue("JOB_DIRECTORY_COLLISION", str(exc))]
        return receipt

    receipt = _base_receipt(input_hashes=input_hashes, seedname=seedname, cores=cores, timeout_seconds=timeout, status="initializing")
    receipt["job"] = {"directory": str(job_directory), "directory_name": job_directory.name}
    try:
        receipt["copied_input_hashes"] = _copy_bound_inputs(manifest, job_directory, seedname, input_hashes)
    except Exception as exc:
        receipt["status"] = "input_copy_failed"
        receipt["errors"] = [_issue("INPUT_COPY_FAILED", f"{type(exc).__name__}: {exc}")]
        return _finish(job_directory, receipt)

    try:
        with acquire_execution_slot():
            try:
                helper, helper_hash, interpreter, interpreter_hash = _synthetic_adapter()
            except Exception as exc:
                receipt["status"] = "synthetic_adapter_unavailable"
                receipt["errors"] = [_issue("SYNTHETIC_ADAPTER_UNAVAILABLE", f"{type(exc).__name__}: {exc}")]
                return _finish(job_directory, receipt)
            receipt["adapter"] = {
                "helper_path": str(helper), "helper_sha256": helper_hash,
                "interpreter_path": str(interpreter), "interpreter_sha256": interpreter_hash,
            }
            _, rechecked_hashes, recheck_errors = _validate_input_contract(manifest)
            if _input_changed(input_hashes, rechecked_hashes, recheck_errors):
                receipt["status"] = "input_changed_before_launch"
                receipt["errors"] = recheck_errors or [_issue("INPUT_CHANGED_BEFORE_LAUNCH", "Input hashes changed after staging and before process creation.")]
                receipt["input_hashes_after_recheck"] = rechecked_hashes
                return _finish(job_directory, receipt)

            stdout_path = job_directory / "runner.stdout.log"
            stderr_path = job_directory / "runner.stderr.log"
            pid_path = job_directory / "synthetic_tree_pids.txt"
            command = [str(interpreter), str(helper), "--scenario", scenario, "--seed", seedname, "--pid-file", str(pid_path)]
            receipt["process"]["stdout_path"] = str(stdout_path)
            receipt["process"]["stderr_path"] = str(stderr_path)
            started = _now()
            process: subprocess.Popen[bytes] | None = None
            termination_kind: str | None = None
            try:
                with stdout_path.open("xb") as stdout, stderr_path.open("xb") as stderr:
                    process = subprocess.Popen(
                        command, cwd=job_directory, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
                        close_fds=True,
                    )
                    receipt["process"]["pid"] = process.pid
                    receipt["process"]["started_utc"] = started
                    deadline = time.monotonic() + timeout
                    while process.poll() is None:
                        if cancel_event is not None and cancel_event.is_set():
                            termination_kind = "cancelled"
                            break
                        if time.monotonic() >= deadline:
                            termination_kind = "timeout"
                            break
                        time.sleep(0.05)
                    if termination_kind is not None:
                        receipt["process"]["termination"] = _terminate_process_tree(process)
                    try:
                        process.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        receipt["status"] = "process_cleanup_failed"
                        receipt["errors"] = [_issue("PROCESS_CLEANUP_FAILED", "Owned synthetic process did not exit after tree termination.")]
                        receipt["process"]["ended_utc"] = _now()
                        return _finish(job_directory, receipt)
            except OSError as exc:
                receipt["status"] = "start_failed"
                receipt["errors"] = [_issue("PROCESS_START_FAILED", f"{type(exc).__name__}: {exc}")]
                receipt["process"]["ended_utc"] = _now()
                return _finish(job_directory, receipt)

            assert process is not None
            receipt["process"]["ended_utc"] = _now()
            receipt["process"]["exit_code"] = process.returncode
            receipt["logs"] = {"stdout_sha256": sha256_file(stdout_path), "stderr_sha256": sha256_file(stderr_path)}
            output = job_directory / f"{seedname}.castep"
            if output.is_file():
                output_hash = sha256_file(output)
                receipt["output"] = {"path": str(output), "sha256": output_hash, "present": True}
                receipt["parser"] = parse_standalone_castep_result(
                    castep_output=output, input_manifest=manifest, expected_output_sha256=output_hash,
                    process_exit_code=process.returncode, termination=termination_kind,
                )
            if termination_kind is not None:
                receipt["status"] = termination_kind
            elif process.returncode != 0:
                receipt["status"] = "nonzero_exit"
            elif not output.is_file():
                receipt["status"] = "output_missing"
                receipt["errors"] = [_issue("OUTPUT_MISSING", "Synthetic helper exited without the required .castep output.")]
            elif receipt["parser"]["status"] != "completed":
                receipt["status"] = "output_parse_failed"
                receipt["errors"] = [_issue("OUTPUT_PARSE_FAILED", "P1 parser did not qualify synthetic output completion.")]
            else:
                receipt["status"] = "qualified_process_control"
                receipt["process_control_qualified"] = True
            return _finish(job_directory, receipt)
    except RuntimeError as exc:
        receipt["status"] = "blocked_lock"
        receipt["errors"] = [_issue("EXECUTION_SLOT_UNAVAILABLE", str(exc))]
        return _finish(job_directory, receipt)
