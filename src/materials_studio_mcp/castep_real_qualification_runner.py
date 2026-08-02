from __future__ import annotations

"""Private, single-use P3-B runner for one reviewed local CASTEP attempt.

This module is deliberately not exposed through MCP. It accepts only the exact
frozen P3-A plan, consumes a hash-bound authorization once, stages immutable
inputs into a new ASCII directory, and launches the fixed MS 2023 wrapper.
"""

import ctypes
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import threading
import time
from typing import Any

from .castep_real_qualification_plan import (
    _CMD,
    _CMD_SHA256,
    _QUALIFICATION_ROOT,
    _RUNCASTEP,
    _RUNCASTEP_SHA256,
    _canonical_sha256,
    _fixed_file,
    validate_real_castep_qualification_plan,
)
from .castep_result_parser import parse_standalone_castep_result
from .castep_standalone_runner import (
    _copy_bound_inputs,
    _tree_termination_fallback,
    _validate_input_contract,
    _verify_staged_inputs,
)
from .conversion_executor import _terminate_process_tree
from .geology_modeling import sha256_file
from .pipeline_config import _pid_exists, acquire_execution_slot


RUN_SCHEMA_VERSION = 1
RUNNER_REVISION = "ms-mcp.private-real-castep-qualification-runner.1.3.0-p3b-r1"
APPROVED_PLAN_SHA256 = "E461D57676903DEA6A19886D1AE85EB28859DC4AE2DC933D9890AA1E8D59C35E"
AUTHORIZATION_ACTION = "execute_one_local_p3b_castep_qualification"
AUTHORIZATION_BASIS = "explicit_user_authorization_in_codex_thread_2026-08-02"
_NONCE = re.compile(r"^[0-9A-F]{64}$")
_SAFE_SEED = re.compile(r"^[A-Za-z0-9_]{1,48}$")
_LICENSE_FAILURE = re.compile(
    r"(?:licen[cs]e).*(?:unavailable|not available|cannot obtain|unable to obtain|denied|checkout failed|failed)",
    re.IGNORECASE,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _issue(code: str, detail: str) -> dict[str, str]:
    return {"code": code, "detail": detail}


def create_single_use_authorization(*, plan_sha256: str, nonce: str) -> dict[str, Any]:
    payload = {
        "schema_version": 1,
        "action": AUTHORIZATION_ACTION,
        "plan_sha256": plan_sha256,
        "cores": 4,
        "timeout_seconds": 600,
        "max_executions": 1,
        "nonce": nonce.upper(),
        "authorization_basis": AUTHORIZATION_BASIS,
    }
    authorization = {**payload, "authorization_sha256": _canonical_sha256(payload)}
    validate_single_use_authorization(authorization)
    return authorization


def validate_single_use_authorization(authorization: dict[str, Any]) -> None:
    allowed = {
        "schema_version", "action", "plan_sha256", "cores", "timeout_seconds",
        "max_executions", "nonce", "authorization_basis", "authorization_sha256",
    }
    if not isinstance(authorization, dict) or set(authorization) != allowed:
        raise ValueError("P3-B authorization fields do not exactly match the reviewed contract")
    payload = {key: value for key, value in authorization.items() if key != "authorization_sha256"}
    if (
        payload.get("schema_version") != 1
        or payload.get("action") != AUTHORIZATION_ACTION
        or payload.get("plan_sha256") != APPROVED_PLAN_SHA256
        or payload.get("cores") != 4
        or payload.get("timeout_seconds") != 600
        or payload.get("max_executions") != 1
        or payload.get("authorization_basis") != AUTHORIZATION_BASIS
        or not isinstance(payload.get("nonce"), str)
        or _NONCE.fullmatch(payload["nonce"]) is None
    ):
        raise ValueError("P3-B authorization does not match the reviewed one-run contract")
    if authorization["authorization_sha256"] != _canonical_sha256(payload):
        raise ValueError("P3-B authorization SHA-256 does not match its canonical payload")


def _load_plan(path: Path) -> dict[str, Any]:
    plan_path = path.resolve(strict=True)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    validate_real_castep_qualification_plan(plan)
    if plan["plan_sha256"] != APPROVED_PLAN_SHA256:
        raise ValueError("P3-B accepts only the exact reviewed P3-A plan")
    if plan["runtime"]["cores"] != 4 or plan["runtime"]["hard_timeout_seconds"] != 600:
        raise ValueError("P3-B plan resource policy has changed")
    return plan


def _create_job_directory(root: Path, seedname: str) -> Path:
    resolved = root.resolve()
    if not str(resolved).isascii() or _SAFE_SEED.fullmatch(seedname) is None:
        raise ValueError("P3-B requires a complete ASCII job path and safe seedname")
    resolved.mkdir(parents=True, exist_ok=True)
    for _ in range(4):
        candidate = resolved / f"p3b_{seedname}_{secrets.token_hex(10)}"
        try:
            candidate.mkdir(exist_ok=False)
            return candidate
        except FileExistsError:
            continue
    raise FileExistsError("P3-B could not create a unique non-overwriting job directory")


def _consume_authorization(root: Path, authorization: dict[str, Any], job_directory: Path) -> Path:
    validate_single_use_authorization(authorization)
    digest = authorization["authorization_sha256"]
    marker_root = root / "authorization_consumed"
    marker_root.mkdir(parents=True, exist_ok=True)
    marker = marker_root / f"{digest}.json"
    record = {
        "schema_version": 1,
        "authorization_sha256": digest,
        "plan_sha256": authorization["plan_sha256"],
        "consumed_utc": _now(),
        "job_directory": str(job_directory),
        "nonce_sha256": hashlib.sha256(authorization["nonce"].encode("ascii")).hexdigest().upper(),
    }
    with marker.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(record, handle, ensure_ascii=True, indent=2, allow_nan=False)
        handle.write("\n")
    return marker


def _snapshot_windows_processes() -> dict[int, tuple[int, str]]:
    if os.name != "nt":
        return {}

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", ctypes.c_ulong), ("cntUsage", ctypes.c_ulong),
            ("th32ProcessID", ctypes.c_ulong), ("th32DefaultHeapID", ctypes.c_void_p),
            ("th32ModuleID", ctypes.c_ulong), ("cntThreads", ctypes.c_ulong),
            ("th32ParentProcessID", ctypes.c_ulong), ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", ctypes.c_ulong), ("szExeFile", ctypes.c_wchar * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_ulong, ctypes.c_ulong]
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    kernel32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32FirstW.restype = ctypes.c_int
    kernel32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    if snapshot in (0, ctypes.c_void_p(-1).value):
        return {}
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(entry)
    result: dict[int, tuple[int, str]] = {}
    try:
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            result[int(entry.th32ProcessID)] = (int(entry.th32ParentProcessID), str(entry.szExeFile))
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return result


def _descendants(root_pid: int, processes: dict[int, tuple[int, str]]) -> dict[int, str]:
    owned = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, (parent, _) in processes.items():
            if parent in owned and pid not in owned:
                owned.add(pid)
                changed = True
    return {pid: processes[pid][1] for pid in owned if pid != root_pid and pid in processes}


def _write_json_exclusive(path: Path, value: dict[str, Any]) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=True, indent=2, allow_nan=False)
        handle.write("\n")


def _read_log(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")[-200000:]


def _terminate_owned_process(process: subprocess.Popen[bytes], receipt: dict[str, Any]) -> None:
    try:
        receipt["process"]["termination"] = _terminate_process_tree(process)
    except Exception as exc:
        receipt["process"]["termination"] = _tree_termination_fallback(process, exc)
        receipt["errors"].append(
            _issue("PROCESS_TREE_TERMINATION_FAILED", f"{type(exc).__name__}: {exc}")
        )


def execute_real_castep_qualification_once(
    *,
    plan_path: Path,
    input_manifest: Path,
    authorization: dict[str, Any],
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Execute exactly one authorized P3-B local qualification attempt."""

    plan = _load_plan(Path(plan_path))
    validate_single_use_authorization(authorization)
    root = _QUALIFICATION_ROOT.resolve()
    if str(root) != plan["runtime"]["qualification_root"] or not str(root).isascii():
        raise ValueError("P3-B qualification root differs from the frozen plan")
    launcher = _fixed_file(_RUNCASTEP, _RUNCASTEP_SHA256, "RunCASTEP launcher")
    command_interpreter = _fixed_file(_CMD, _CMD_SHA256, "Windows command interpreter")
    if launcher["sha256"] != plan["runtime"]["launcher"]["sha256"]:
        raise PermissionError("P3-B launcher differs from the plan")
    if command_interpreter["sha256"] != plan["runtime"]["command_interpreter"]["sha256"]:
        raise PermissionError("P3-B command interpreter differs from the plan")

    manifest = Path(input_manifest).resolve()
    seedname, input_hashes, input_errors = _validate_input_contract(manifest)
    frozen_hashes = plan["input"]["hashes"]
    if (
        input_errors
        or seedname != plan["input"]["seedname"]
        or any(input_hashes.get(key) != value for key, value in frozen_hashes.items() if key != "manifest_path")
    ):
        raise ValueError("P3-B input contract differs from the frozen plan")

    job_directory = _create_job_directory(root, seedname)
    receipt: dict[str, Any] = {
        "schema_version": RUN_SCHEMA_VERSION,
        "runner_revision": RUNNER_REVISION,
        "status": "initializing",
        "qualification_only": True,
        "production_science_released": False,
        "plan_sha256": plan["plan_sha256"],
        "authorization_sha256": authorization["authorization_sha256"],
        "job_directory": str(job_directory),
        "seedname": seedname,
        "cores": 4,
        "timeout_seconds": 600,
        "input_hashes": input_hashes,
        "copied_input_hashes": {},
        "staged_input_hashes_before_launch": {},
        "staged_input_hashes_after_exit": {},
        "launcher": launcher,
        "command_interpreter": command_interpreter,
        "process": {
            "pid": None, "started_utc": None, "ended_utc": None,
            "exit_code": None, "termination": None,
        },
        "observed_descendant_pids": {},
        "owned_processes_remaining": [],
        "logs": {},
        "artifacts": [],
        "parser": None,
        "license_evidence": "unverified",
        "errors": [],
    }
    receipt_path = job_directory / "p3b_runner_receipt.json"
    process: subprocess.Popen[bytes] | None = None
    observed: dict[int, str] = {}
    termination_kind: str | None = None

    try:
        receipt["copied_input_hashes"] = _copy_bound_inputs(
            manifest, job_directory, seedname, input_hashes
        )
        staged, staged_errors = _verify_staged_inputs(
            job_directory, receipt["copied_input_hashes"]
        )
        receipt["staged_input_hashes_before_launch"] = staged
        if staged_errors:
            receipt["status"] = "staged_input_changed_before_launch"
            receipt["errors"] = staged_errors
            _write_json_exclusive(receipt_path, receipt)
            return {**receipt, "receipt_path": str(receipt_path)}

        reviewed_command = f'call "{launcher["path"]}" -np 4 {seedname}'
        command = [command_interpreter["path"], "/d", "/s", "/c", reviewed_command]
        if command != plan["runtime"]["command_preview"]:
            raise ValueError("P3-B rendered command differs from the frozen plan")
        receipt["command_sha256"] = _canonical_sha256(command)

        with acquire_execution_slot():
            marker = _consume_authorization(root, authorization, job_directory)
            receipt["authorization_consumption_marker"] = str(marker)
            stdout_path = job_directory / "runner.stdout.log"
            stderr_path = job_directory / "runner.stderr.log"
            with stdout_path.open("xb") as stdout, stderr_path.open("xb") as stderr:
                process = subprocess.Popen(
                    command,
                    cwd=job_directory,
                    shell=False,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
                    close_fds=True,
                )
                receipt["process"]["pid"] = process.pid
                receipt["process"]["started_utc"] = _now()
                deadline = time.monotonic() + 600
                while process.poll() is None:
                    observed.update(_descendants(process.pid, _snapshot_windows_processes()))
                    if cancel_event is not None and cancel_event.is_set():
                        termination_kind = "cancelled"
                        break
                    if time.monotonic() >= deadline:
                        termination_kind = "timeout"
                        break
                    time.sleep(0.2)
                if termination_kind is not None:
                    _terminate_owned_process(process, receipt)
                process.wait(timeout=30)
    except Exception as exc:
        if process is not None and process.poll() is None:
            _terminate_owned_process(process, receipt)
            try:
                process.wait(timeout=30)
            except Exception as wait_exc:
                receipt["errors"].append(
                    _issue("PROCESS_WAIT_FAILED", f"{type(wait_exc).__name__}: {wait_exc}")
                )
        receipt["status"] = "runner_error"
        receipt["errors"].append(_issue("RUNNER_ERROR", f"{type(exc).__name__}: {exc}"))

    receipt["observed_descendant_pids"] = {
        str(pid): name for pid, name in sorted(observed.items())
    }
    receipt["process"]["ended_utc"] = _now()
    receipt["process"]["exit_code"] = process.returncode if process is not None else None
    receipt["owned_processes_remaining"] = [
        pid for pid in observed if _pid_exists(pid)
    ]
    stdout_path = job_directory / "runner.stdout.log"
    stderr_path = job_directory / "runner.stderr.log"
    receipt["logs"] = {
        "stdout_path": str(stdout_path),
        "stdout_sha256": sha256_file(stdout_path) if stdout_path.is_file() else None,
        "stderr_path": str(stderr_path),
        "stderr_sha256": sha256_file(stderr_path) if stderr_path.is_file() else None,
    }

    try:
        staged_after, staged_after_errors = _verify_staged_inputs(
            job_directory, receipt["copied_input_hashes"]
        )
        receipt["staged_input_hashes_after_exit"] = staged_after
        receipt["errors"].extend(staged_after_errors)

        output = job_directory / f"{seedname}.castep"
        if output.is_file():
            output_hash = sha256_file(output)
            receipt["parser"] = parse_standalone_castep_result(
                castep_output=output,
                input_manifest=manifest,
                expected_output_sha256=output_hash,
                process_exit_code=receipt["process"]["exit_code"],
                termination=termination_kind,
            )
        for artifact in sorted(
            path for path in job_directory.iterdir()
            if path.is_file() and path.name != receipt_path.name
        ):
            receipt["artifacts"].append({
                "name": artifact.name,
                "bytes": artifact.stat().st_size,
                "sha256": sha256_file(artifact),
            })

        log_text = _read_log(stdout_path) + "\n" + _read_log(stderr_path)
        parser_classification = (
            receipt["parser"].get("classification") if receipt["parser"] else None
        )
        if _LICENSE_FAILURE.search(log_text) or parser_classification == "license_unavailable":
            receipt["license_evidence"] = "license_unavailable"
        elif receipt["parser"] and receipt["parser"].get("status") == "completed":
            receipt["license_evidence"] = "calculation_completed_license_inferred"

        remaining = receipt["owned_processes_remaining"]
        if termination_kind is not None:
            receipt["status"] = termination_kind
        elif receipt["status"] == "runner_error":
            pass
        elif receipt["errors"]:
            receipt["status"] = "failed"
        elif remaining:
            receipt["status"] = "owned_processes_remaining"
        elif receipt["process"]["exit_code"] != 0:
            receipt["status"] = "nonzero_exit"
        elif receipt["parser"] is None:
            receipt["status"] = "output_missing"
        elif receipt["parser"].get("status") != "completed":
            receipt["status"] = "output_parse_failed"
        else:
            receipt["status"] = "qualification_pass"
    except Exception as exc:
        receipt["status"] = "postprocess_error"
        receipt["errors"].append(
            _issue("POSTPROCESS_ERROR", f"{type(exc).__name__}: {exc}")
        )

    try:
        _write_json_exclusive(receipt_path, receipt)
    except Exception as exc:
        receipt["status"] = "receipt_write_failed"
        receipt["errors"].append(
            _issue("RECEIPT_WRITE_FAILED", f"{type(exc).__name__}: {exc}")
        )
    return {**receipt, "receipt_path": str(receipt_path)}
