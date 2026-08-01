from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
from typing import Any
import uuid


TASK_SCHEMA_VERSION = 1
TASK_ROOT_ENV = "MATERIALS_STUDIO_MCP_TASK_ROOT"
DEFAULT_TASK_ROOT = Path(r"E:\ms_mcp\ms_mcp_jobs\tasks")
ASYNC_TOOL_ALLOWLIST = frozenset(
    {
        "md_export_xsd_to_car_mdf_checked",
        "md_convert_to_lammps_checked",
        "ms_geology_import_crystal_parent",
        "ms_geology_build_periodic_slab_cell",
        "ms_pack_periodic_aqueous_nacl",
        "md_build_clayff_spce_nacl_lammps",
        "ms_forcite_calculation_checked",
        "ms_geology_build_supercell",
        "ms_geology_enumerate_surface_terminations",
        "ms_geology_apply_substitutions",
        "ms_geology_place_counterions",
        "ms_geology_apply_hydroxylation_ledger",
    }
)
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _task_root(*, create: bool = False) -> Path:
    root = Path(os.environ.get(TASK_ROOT_ENV, DEFAULT_TASK_ROOT)).expanduser().resolve()
    if create:
        root.mkdir(parents=True, exist_ok=True)
    return root


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest().upper()


def _owner_hash(capability: str) -> str:
    return hashlib.sha256(capability.encode("ascii")).hexdigest().upper()


def _task_directory(task_id: str) -> Path:
    try:
        normalized = str(uuid.UUID(task_id))
    except (ValueError, TypeError) as exc:
        raise ValueError("task_id must be a UUID") from exc
    return _task_root() / normalized


def _record_path(task_id: str) -> Path:
    return _task_directory(task_id) / "task.json"


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_record(task_id: str) -> dict[str, Any]:
    path = _record_path(task_id)
    if not path.is_file():
        raise FileNotFoundError(f"Task does not exist: {task_id}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != TASK_SCHEMA_VERSION or data.get("task_id") != task_id:
        raise ValueError("Task record schema or identity is invalid")
    return data


def _authorize(record: dict[str, Any], owner_capability: str) -> None:
    if not isinstance(owner_capability, str) or not secrets.compare_digest(
        record["owner_capability_sha256"], _owner_hash(owner_capability)
    ):
        raise PermissionError("Task owner capability is invalid")


def _process_is_running(pid: int) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _scheduler_executable() -> Path:
    executable = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "schtasks.exe"
    if not executable.is_file():
        raise FileNotFoundError(f"Windows Task Scheduler executable is missing: {executable}")
    return executable


def _scheduler_task_name(task_id: str) -> str:
    return f"MaterialsStudioMCP-{uuid.UUID(task_id)}"


def _delete_scheduled_worker(task_id: str) -> dict[str, Any]:
    if os.name != "nt":
        return {"status": "not_applicable"}
    completed = subprocess.run(
        [str(_scheduler_executable()), "/Delete", "/TN", _scheduler_task_name(task_id), "/F"],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30, check=False,
    )
    return {"exit_code": completed.returncode, "stdout": completed.stdout[-2000:], "stderr": completed.stderr[-2000:]}


def _launch_worker(task_id: str, task_dir: Path) -> dict[str, Any]:
    if os.name == "nt":
        scheduler = _scheduler_executable()
        task_name = _scheduler_task_name(task_id)
        start_time = (datetime.now().astimezone() + timedelta(minutes=10)).strftime("%H:%M")
        command = f'"{sys.executable}" -m materials_studio_mcp.task_manager run {task_id}'
        created = subprocess.run(
            [str(scheduler), "/Create", "/TN", task_name, "/TR", command, "/SC", "ONCE", "/ST", start_time, "/F"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30, check=False,
        )
        if created.returncode != 0:
            raise RuntimeError(f"Task Scheduler create failed: {(created.stderr or created.stdout)[-2000:]}")
        started = subprocess.run(
            [str(scheduler), "/Run", "/TN", task_name],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30, check=False,
        )
        if started.returncode != 0:
            _delete_scheduled_worker(task_id)
            raise RuntimeError(f"Task Scheduler run failed: {(started.stderr or started.stdout)[-2000:]}")
        return {
            "kind": "windows_task_scheduler",
            "task_name": task_name,
            "create_exit_code": created.returncode,
            "run_exit_code": started.returncode,
        }
    stdout_handle = (task_dir / "worker.stdout.log").open("ab")
    stderr_handle = (task_dir / "worker.stderr.log").open("ab")
    try:
        process = subprocess.Popen(
            [sys.executable, "-m", "materials_studio_mcp.task_manager", "run", task_id],
            stdin=subprocess.DEVNULL, stdout=stdout_handle, stderr=stderr_handle,
            close_fds=True, start_new_session=True,
        )
    finally:
        stdout_handle.close()
        stderr_handle.close()
    return {"kind": "detached_process", "worker_pid": process.pid}


def validate_task_request(tool_name: str, parameters: dict[str, Any]) -> dict[str, Any]:
    if tool_name not in ASYNC_TOOL_ALLOWLIST:
        raise ValueError(f"Async task tool is not allowed: {tool_name}")
    if not isinstance(parameters, dict):
        raise ValueError("parameters must be an object")
    forbidden = {"confirmation_token", "dry_run"} & set(parameters)
    if forbidden:
        raise ValueError(f"Task parameters contain reserved fields: {sorted(forbidden)}")
    return {
        "tool_name": tool_name,
        "parameters": parameters,
        "parameters_sha256": _sha256(parameters),
    }


def submit_task(tool_name: str, parameters: dict[str, Any], *, retry_of: str | None = None) -> dict[str, Any]:
    request = validate_task_request(tool_name, parameters)
    task_id = str(uuid.uuid4())
    owner_capability = secrets.token_urlsafe(32)
    task_root = _task_root(create=True)
    task_dir = task_root / task_id
    task_dir.mkdir(parents=False, exist_ok=False)
    record = {
        "schema_version": TASK_SCHEMA_VERSION,
        "task_id": task_id,
        "status": "queued",
        "tool_name": tool_name,
        "parameters": parameters,
        "parameters_sha256": request["parameters_sha256"],
        "owner_capability_sha256": _owner_hash(owner_capability),
        "created_at": _now(),
        "updated_at": _now(),
        "retry_of": retry_of,
        "worker_pid": None,
        "result_path": str(task_dir / "result.json"),
        "stdout_path": str(task_dir / "worker.stdout.log"),
        "stderr_path": str(task_dir / "worker.stderr.log"),
    }
    _write_json_atomic(task_dir / "task.json", record)
    (task_dir / "worker.stdout.log").touch(exist_ok=False)
    (task_dir / "worker.stderr.log").touch(exist_ok=False)
    try:
        launcher = _launch_worker(task_id, task_dir)
    except Exception:
        record["status"] = "failed"
        record["updated_at"] = _now()
        record["error"] = "Worker process could not be started"
        _write_json_atomic(task_dir / "task.json", record)
        raise
    latest = _read_record(task_id)
    latest["launcher"] = launcher
    if latest["status"] == "queued" and launcher.get("worker_pid"):
        latest["status"] = "running"
        latest["worker_pid"] = launcher["worker_pid"]
    latest["updated_at"] = _now()
    _write_json_atomic(task_dir / "task.json", latest)
    return {
        "status": "submitted",
        "task_id": task_id,
        "owner_capability": owner_capability,
        "owner_capability_notice": "Returned once; store it securely. Only its SHA-256 is persisted.",
        "tool_name": tool_name,
        "parameters_sha256": request["parameters_sha256"],
        "task_record": str(task_dir / "task.json"),
    }


def query_task(task_id: str, owner_capability: str) -> dict[str, Any]:
    record = _read_record(task_id)
    _authorize(record, owner_capability)
    if record["status"] == "running" and not _process_is_running(record.get("worker_pid")):
        record["status"] = "failed"
        record["updated_at"] = _now()
        record["error"] = "Worker process exited without writing a terminal task state"
        _write_json_atomic(_record_path(task_id), record)
    public = {key: value for key, value in record.items() if key != "owner_capability_sha256"}
    result_path = Path(record["result_path"])
    if result_path.is_file():
        public["result"] = json.loads(result_path.read_text(encoding="utf-8"))
        public["result_sha256"] = hashlib.sha256(result_path.read_bytes()).hexdigest().upper()
    return public


def cancel_task(task_id: str, owner_capability: str) -> dict[str, Any]:
    record = _read_record(task_id)
    _authorize(record, owner_capability)
    if record["status"] in TERMINAL_STATUSES:
        return {"status": record["status"], "task_id": task_id, "already_terminal": True}
    pid = record.get("worker_pid")
    if isinstance(pid, int) and pid > 0:
        taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
        if not taskkill.is_file():
            raise FileNotFoundError(f"Fixed taskkill executable is missing: {taskkill}")
        completed = subprocess.run(
            [str(taskkill), "/PID", str(pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        termination = {"exit_code": completed.returncode, "stdout": completed.stdout[-2000:], "stderr": completed.stderr[-2000:]}
    else:
        termination = {"exit_code": None, "detail": "No worker PID was recorded"}
    record["status"] = "cancelled"
    record["updated_at"] = _now()
    record["termination"] = termination
    record["scheduler_cleanup"] = _delete_scheduled_worker(task_id)
    _write_json_atomic(_record_path(task_id), record)
    return {"status": "cancelled", "task_id": task_id, "termination": termination}


def retry_task(task_id: str, owner_capability: str) -> dict[str, Any]:
    record = _read_record(task_id)
    _authorize(record, owner_capability)
    if record["status"] not in {"failed", "cancelled"}:
        raise ValueError("Only failed or cancelled tasks may be retried")
    parameters = dict(record["parameters"])
    old_key = parameters.get("idempotency_key")
    if isinstance(old_key, str) and old_key:
        suffix = f".retry.{uuid.uuid4().hex[:12]}"
        parameters["idempotency_key"] = old_key[: 128 - len(suffix)] + suffix
    return submit_task(record["tool_name"], parameters, retry_of=task_id)


def run_worker(task_id: str) -> int:
    record = _read_record(task_id)
    if record["status"] == "cancelled":
        return 2
    record["status"] = "running"
    record["worker_pid"] = os.getpid()
    record["updated_at"] = _now()
    _write_json_atomic(_record_path(task_id), record)
    try:
        from . import server

        function = getattr(server, record["tool_name"])
        parameters = dict(record["parameters"])
        confirmation = server.confirmation_manager.issue(record["tool_name"], parameters, 300)
        result = function(**parameters, dry_run=False, confirmation_token=confirmation["confirmation_token"])
        result_path = Path(record["result_path"])
        _write_json_atomic(result_path, result)
        latest = _read_record(task_id)
        if latest["status"] != "cancelled":
            latest["status"] = "succeeded" if result.get("ok") is True else "failed"
            latest["updated_at"] = _now()
            latest["result_sha256"] = hashlib.sha256(result_path.read_bytes()).hexdigest().upper()
            _write_json_atomic(_record_path(task_id), latest)
        return 0 if result.get("ok") is True else 1
    except Exception as exc:
        latest = _read_record(task_id)
        if latest["status"] != "cancelled":
            latest["status"] = "failed"
            latest["updated_at"] = _now()
            latest["error"] = f"{type(exc).__name__}: {exc}"
            _write_json_atomic(_record_path(task_id), latest)
        return 1
    finally:
        _delete_scheduled_worker(task_id)


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "run":
        return run_worker(sys.argv[2])
    raise SystemExit("Usage: python -m materials_studio_mcp.task_manager run <task-id>")


if __name__ == "__main__":
    raise SystemExit(main())
