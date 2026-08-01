from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any


def discover_project_root(module_file: str | Path = __file__) -> Path:
    configured = os.environ.get("MATERIALS_STUDIO_MCP_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    required = {"software.local.json", "policy.json", "project-manifest.template.json"}
    module_path = Path(module_file).expanduser().resolve()
    for parent in module_path.parents:
        config = parent / "config"
        if config.is_dir() and all((config / name).is_file() for name in required):
            return parent
    raise FileNotFoundError(
        f"Could not discover a complete Materials Studio MCP config root above: {module_path}"
    )


PROJECT_ROOT = discover_project_root()
CONFIG_ROOT = PROJECT_ROOT / "config"


def _resolved(path: str | Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _assert_no_reparse_points(path: Path, roots: tuple[Path, ...]) -> None:
    """Reject links/junctions in an in-workspace path before following them."""
    lexical = Path(os.path.abspath(os.path.expanduser(str(path))))
    lexical_root = next((root for root in roots if _is_within(lexical, root)), None)
    if lexical_root is None:
        return
    current = lexical_root
    for part in lexical.relative_to(lexical_root).parts:
        current = current / part
        if not current.exists():
            continue
        stat = current.lstat()
        is_reparse = bool(getattr(stat, "st_file_attributes", 0) & 0x400)
        if current.is_symlink() or is_reparse:
            raise PermissionError(f"Symbolic links and reparse points are not allowed in workspace paths: {current}")


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"Configuration root must be an object: {path}")
    return data


def _strict_keys(data: dict[str, Any], *, allowed: set[str], required: set[str], label: str) -> None:
    unknown = set(data) - allowed
    missing = required - set(data)
    if unknown:
        raise ValueError(f"Unknown {label} configuration keys: {sorted(unknown)}")
    if missing:
        raise ValueError(f"Missing {label} configuration keys: {sorted(missing)}")


def _validate_config_schema(software: dict[str, Any], policy: dict[str, Any]) -> None:
    _strict_keys(software, allowed={"schema_version", "materials_studio", "lammps", "mpi", "vmd", "packmol"},
                 required={"schema_version", "materials_studio", "lammps", "mpi", "vmd", "packmol"}, label="software")
    software_keys = {
        "materials_studio": ({"root", "run_mat_script"}, {"root", "run_mat_script"}),
        "lammps": ({"executable", "msi2lmp", "frc_files"}, {"executable", "msi2lmp", "frc_files"}),
        "mpi": ({"executable", "default_processes"}, {"executable", "default_processes"}),
        "vmd": ({"executable"}, {"executable"}),
        "packmol": ({"executable", "shell"}, {"executable"}),
    }
    for section, (allowed, required) in software_keys.items():
        value = software.get(section)
        if not isinstance(value, dict):
            raise ValueError(f"software.{section} must be an object")
        _strict_keys(value, allowed=allowed, required=required, label=f"software.{section}")
    _strict_keys(policy, allowed={"schema_version", "workspace_roots", "scratch_root", "allowed_executable_paths", "limits", "execution", "preflight"},
                 required={"schema_version", "workspace_roots", "scratch_root", "allowed_executable_paths", "limits", "execution", "preflight"}, label="policy")
    nested = {
        "limits": {"default_timeout_seconds", "max_timeout_seconds", "max_parallel_jobs", "max_mpi_processes"},
        "execution": {"overwrite_existing_outputs", "retain_failed_job_directories", "require_preflight_before_production", "require_confirmation_for_production"},
        "preflight": {"require_lammps_run_zero", "require_short_minimization", "require_short_dynamics", "reject_lost_atoms", "reject_nan_or_infinite_energy", "require_neutrality_check", "require_forcefield_coverage_check"},
    }
    for section, allowed in nested.items():
        value = policy.get(section)
        if not isinstance(value, dict):
            raise ValueError(f"policy.{section} must be an object")
        _strict_keys(value, allowed=allowed, required=allowed, label=f"policy.{section}")
    if policy.get("schema_version") != 1 or software.get("schema_version") != 1:
        raise ValueError("Only configuration schema_version 1 is supported")
    if not isinstance(policy.get("workspace_roots"), list) or not policy["workspace_roots"]:
        raise ValueError("policy.workspace_roots must be a non-empty array")


def load_pipeline_config() -> dict[str, Any]:
    software_path = Path(os.environ.get("MD_PIPELINE_SOFTWARE_CONFIG", CONFIG_ROOT / "software.local.json"))
    policy_path = Path(os.environ.get("MD_PIPELINE_POLICY_CONFIG", CONFIG_ROOT / "policy.json"))
    software = _load_json(software_path)
    policy = _load_json(policy_path)
    _validate_config_schema(software, policy)
    overrides = {
        ("materials_studio", "root"): os.environ.get("MATERIALS_STUDIO_ROOT"),
        ("lammps", "executable"): os.environ.get("LAMMPS_EXECUTABLE"),
        ("mpi", "executable"): os.environ.get("MPIEXEC_EXECUTABLE"),
        ("vmd", "executable"): os.environ.get("VMD_EXECUTABLE"),
        ("packmol", "executable"): os.environ.get("PACKMOL_EXECUTABLE"),
        ("packmol", "shell"): os.environ.get("PACKMOL_SHELL"),
    }
    for (section, key), value in overrides.items():
        if value:
            software.setdefault(section, {})[key] = value
    if os.environ.get("MATERIALS_STUDIO_ROOT") and not os.environ.get("RUN_MAT_SCRIPT"):
        software["materials_studio"]["run_mat_script"] = str(
            Path(software["materials_studio"]["root"]) / "etc" / "Scripting" / "bin" / "RunMatScript.bat"
        )
    if os.environ.get("RUN_MAT_SCRIPT"):
        software["materials_studio"]["run_mat_script"] = os.environ["RUN_MAT_SCRIPT"]
    return {"software": software, "policy": policy, "paths": {"software": str(software_path), "policy": str(policy_path)}}


def policy_roots(config: dict[str, Any] | None = None) -> tuple[Path, ...]:
    loaded = config or load_pipeline_config()
    configured = list(loaded["policy"].get("workspace_roots", []))
    scratch = loaded["policy"].get("scratch_root")
    if scratch:
        configured.append(scratch)
    roots = tuple(dict.fromkeys(_resolved(item) for item in configured))
    if not roots:
        raise ValueError("policy.workspace_roots must contain at least one root")
    return roots


def resolve_workspace_path(path: str | Path, *, must_exist: bool = False,
                           config: dict[str, Any] | None = None) -> Path:
    roots = policy_roots(config)
    candidate = Path(path).expanduser()
    _assert_no_reparse_points(candidate, roots)
    resolved = _resolved(candidate)
    if not any(_is_within(resolved, root) for root in roots):
        raise PermissionError(f"Path is outside configured workspace roots: {resolved}")
    if must_exist and not resolved.exists():
        raise FileNotFoundError(resolved)
    return resolved


def resolve_output_path(path: str | Path, *, config: dict[str, Any] | None = None) -> Path:
    loaded = config or load_pipeline_config()
    resolved = resolve_workspace_path(path, config=loaded)
    if resolved.exists() and not loaded["policy"].get("execution", {}).get("overwrite_existing_outputs", False):
        raise FileExistsError(f"Refusing to overwrite existing output: {resolved}")
    return resolved


def bounded_timeout(requested: int | None, *, config: dict[str, Any] | None = None) -> int:
    loaded = config or load_pipeline_config()
    limits = loaded["policy"].get("limits", {})
    value = limits.get("default_timeout_seconds", 120) if requested is None else requested
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("timeout_seconds must be a positive integer")
    maximum = int(limits.get("max_timeout_seconds", 3600))
    if value > maximum:
        raise ValueError(f"timeout_seconds exceeds policy maximum of {maximum}")
    return value


def bounded_mpi_processes(requested: int | None, *, config: dict[str, Any] | None = None) -> int:
    loaded = config or load_pipeline_config()
    default = int(loaded["software"].get("mpi", {}).get("default_processes", 1))
    value = default if requested is None else requested
    maximum = int(loaded["policy"].get("limits", {}).get("max_mpi_processes", 1))
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"MPI process count must be between 1 and {maximum}")
    return value


def approved_executable(path: str | Path, *, config: dict[str, Any] | None = None) -> Path:
    loaded = config or load_pipeline_config()
    resolved = _resolved(path)
    approved = {_resolved(item) for item in loaded["policy"].get("allowed_executable_paths", [])}
    if resolved not in approved:
        raise PermissionError(f"Executable is not on the fixed path allowlist: {resolved}")
    if not resolved.is_file():
        raise FileNotFoundError(resolved)
    return resolved


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        process_query_limited_information = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(process_query_limited_information, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        # Access denied is treated as alive; invalid PID is not.
        return ctypes.get_last_error() == 5
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


@contextmanager
def acquire_execution_slot(*, config: dict[str, Any] | None = None):
    """Acquire one filesystem-backed slot shared by all MCP server processes."""
    loaded = config or load_pipeline_config()
    maximum = int(loaded["policy"].get("limits", {}).get("max_parallel_jobs", 1))
    if maximum < 1:
        raise ValueError("policy max_parallel_jobs must be at least 1")
    scratch = resolve_workspace_path(loaded["policy"]["scratch_root"], config=loaded)
    lock_root = scratch / ".execution_locks"
    lock_root.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    acquired: Path | None = None
    for index in range(maximum):
        candidate = lock_root / f"slot-{index}.lock"
        payload = {"pid": os.getpid(), "token": token, "created_unix": time.time()}
        try:
            fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            acquired = candidate
            break
        except FileExistsError:
            try:
                owner = json.loads(candidate.read_text(encoding="utf-8"))
                if not _pid_exists(int(owner.get("pid", -1))):
                    candidate.unlink(missing_ok=True)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pass
    if acquired is None:
        raise RuntimeError(f"Cross-process parallel job limit reached ({maximum})")
    try:
        yield acquired
    finally:
        try:
            owner = json.loads(acquired.read_text(encoding="utf-8"))
            if owner.get("token") == token:
                acquired.unlink(missing_ok=True)
        except (OSError, json.JSONDecodeError):
            pass


def _probe_path(label: str, value: str | None, *, required: bool = True) -> dict[str, Any]:
    path = Path(value) if value else None
    exists = bool(path and path.is_file())
    return {"name": label, "required": required, "configured_path": str(path) if path else None,
            "exists": exists, "status": "ready" if exists else ("missing" if required else "optional_missing")}


def _probe_directory(label: str, value: str | None, *, required: bool = True) -> dict[str, Any]:
    path = Path(value) if value else None
    exists = bool(path and path.is_dir())
    return {"name": label, "required": required, "configured_path": str(path) if path else None,
            "exists": exists, "status": "ready" if exists else ("missing" if required else "optional_missing")}


def _command_preview(executable: str, arguments: list[str], timeout_seconds: int = 15) -> dict[str, Any]:
    try:
        completed = subprocess.run([executable, *arguments], input="", capture_output=True, text=True, errors="replace",
                                   timeout=timeout_seconds, check=False)
        output = (completed.stdout + "\n" + completed.stderr).strip()
        return {"exit_code": completed.returncode, "output_preview": output[:2000]}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"exit_code": None, "error": str(exc)}


def pipeline_health_check(run_version_probes: bool = True) -> dict[str, Any]:
    config = load_pipeline_config()
    software = config["software"]
    ms, lammps = software.get("materials_studio", {}), software.get("lammps", {})
    checks = [
        _probe_path("Materials Studio RunMatScript", ms.get("run_mat_script")),
        _probe_path("LAMMPS", lammps.get("executable")),
        _probe_path("msi2lmp", lammps.get("msi2lmp")),
        _probe_directory("msi2lmp forcefield library", lammps.get("frc_files")),
        _probe_path("Microsoft MPI", software.get("mpi", {}).get("executable")),
        _probe_path("VMD", software.get("vmd", {}).get("executable")),
        _probe_path("Packmol", software.get("packmol", {}).get("executable"), required=False),
        _probe_path("Packmol shell", software.get("packmol", {}).get("shell"), required=False),
    ]
    by_name = {item["name"]: item for item in checks}
    if run_version_probes and by_name["LAMMPS"]["exists"]:
        by_name["LAMMPS"]["probe"] = _command_preview(lammps["executable"], ["-h"])
    if run_version_probes and by_name["msi2lmp"]["exists"]:
        by_name["msi2lmp"]["probe"] = _command_preview(lammps["msi2lmp"], ["-h"])
    if run_version_probes and by_name["Microsoft MPI"]["exists"]:
        by_name["Microsoft MPI"]["probe"] = _command_preview(software["mpi"]["executable"], ["-help"])
    required_ready = all(item["exists"] for item in checks if item["required"])
    optional_missing = [item["name"] for item in checks if not item["required"] and not item["exists"]]
    return {
        "status": "ready" if required_ready and not optional_missing else ("degraded" if required_ready else "not_ready"),
        "ready_for_ms_lammps_vmd": required_ready,
        "ready_for_packing_workflows": by_name["Packmol"]["exists"] and by_name["Packmol shell"]["exists"],
        "checks": checks, "configuration": config["paths"], "policy_summary": config["policy"].get("limits", {}),
        "next_actions": ["Install or compile Packmol and set packmol.executable"] if optional_missing else []
    }
