from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from .pipeline_config import CONFIG_ROOT, load_pipeline_config
from .science_contract import validate_science_contract


PROJECT_DIRECTORIES = (
    "request", "model", "forcefield", "conversion", "lammps/input", "lammps/restart",
    "lammps/logs", "lammps/trajectory", "analysis", "vmd", "reports",
)
PROJECT_STATUSES = {"draft", "specified", "modelled", "converted", "preflight_passed", "production", "validated", "failed"}
PROJECT_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"specified", "failed"}),
    "specified": frozenset({"modelled", "failed"}),
    "modelled": frozenset({"converted", "failed"}),
    "converted": frozenset({"preflight_passed", "failed"}),
    "preflight_passed": frozenset({"production", "failed"}),
    "production": frozenset({"validated", "failed"}),
    "validated": frozenset(),
    "failed": frozenset(),
}
QUALITY_GATE_STATUSES = {"pending", "pass", "fail", "blocked"}
CALLER_SETTABLE_GATE_STATUSES = {"pending", "fail", "blocked"}
MANIFEST_SCHEMA_VERSION = 1
MANIFEST_REQUIRED_KEYS = {
    "schema_version", "project", "model_specification", "forcefield",
    "artifacts", "quality_gates", "provenance",
}


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _safe_project_id(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip()).strip("._-")
    if not normalized:
        raise ValueError("project_id must contain at least one letter or number")
    return normalized[:80]


def _allowed_roots() -> list[Path]:
    roots = load_pipeline_config()["policy"].get("workspace_roots", [])
    return [Path(item).resolve() for item in roots]


def _ensure_allowed(path: Path) -> Path:
    resolved = path.resolve()
    if not any(resolved == root or root in resolved.parents for root in _allowed_roots()):
        raise PermissionError(f"Path is outside configured workspace_roots: {resolved}")
    return resolved


def _manifest_path(project_directory: str | Path) -> Path:
    directory = _ensure_allowed(Path(project_directory))
    path = directory / "manifest.json"
    if not path.is_file():
        raise FileNotFoundError(f"Project manifest not found: {path}")
    return path


def _read_manifest(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"Manifest root must be an object: {path}")
    _validate_manifest_schema(data, path)
    return data


def _validate_manifest_schema(data: dict[str, Any], path: Path | None = None) -> None:
    """Fail closed on an unsupported or structurally incomplete manifest.

    This is deliberately a small compatibility guard, not a permissive migration.
    Schema migrations must be explicit so older projects are never silently
    interpreted with newer state or quality-gate semantics.
    """
    label = f" ({path})" if path is not None else ""
    if data.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported project manifest schema_version{label}: "
            f"expected {MANIFEST_SCHEMA_VERSION}, got {data.get('schema_version')!r}"
        )
    missing = MANIFEST_REQUIRED_KEYS - set(data)
    if missing:
        raise ValueError(f"Missing project manifest keys{label}: {sorted(missing)}")
    project = data.get("project")
    if not isinstance(project, dict):
        raise ValueError(f"project must be an object{label}")
    if project.get("status") not in PROJECT_STATUSES:
        raise ValueError(f"project.status is missing or invalid{label}")
    if not isinstance(data.get("artifacts"), list):
        raise ValueError(f"artifacts must be an array{label}")
    if not isinstance(data.get("quality_gates"), dict):
        raise ValueError(f"quality_gates must be an object{label}")


def _software_provenance_summary(software: dict[str, Any]) -> dict[str, Any]:
    """Record reproducibility metadata without persisting local executable paths."""
    summary: dict[str, Any] = {}
    for name in ("materials_studio", "lammps", "mpi", "vmd", "packmol"):
        section = software.get(name, {})
        summary[name] = {
            "configured": any(
                bool(section.get(key))
                for key in ("root", "run_mat_script", "executable", "msi2lmp")
            )
        }
        if section.get("version") not in (None, ""):
            summary[name]["version"] = str(section["version"])
    return summary


def _write_manifest(path: Path, data: dict[str, Any]) -> None:
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


@contextmanager
def _manifest_lock(path: Path, timeout_seconds: float = 10.0):
    """Serialize manifest read-modify-write transactions across processes."""
    lock_path = path.with_name(f".{path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
    deadline = time.monotonic() + timeout_seconds
    acquired = False
    try:
        while not acquired:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Timed out acquiring manifest lock: {lock_path}")
                time.sleep(0.02)
        yield
    finally:
        if acquired:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


@contextmanager
def _locked_manifest(path: Path):
    with _manifest_lock(path):
        manifest = _read_manifest(path)
        yield manifest
        _write_manifest(path, manifest)


def initialize_project(project_id: str, title: str, projects_root: str | None = None) -> dict[str, Any]:
    safe_id = _safe_project_id(project_id)
    default_root = _allowed_roots()[0] / "projects"
    root = _ensure_allowed(Path(projects_root) if projects_root else default_root)
    project_dir = _ensure_allowed(root / safe_id)
    if project_dir.exists():
        raise FileExistsError(f"Project already exists; refusing to overwrite: {project_dir}")

    template = _read_manifest(CONFIG_ROOT / "project-manifest.template.json")
    manifest = deepcopy(template)
    manifest["project"].update({"id": safe_id, "title": title.strip() or safe_id, "created_at": _now(), "updated_at": _now()})
    manifest["provenance"]["software"] = _software_provenance_summary(
        load_pipeline_config()["software"]
    )
    try:
        for relative in PROJECT_DIRECTORIES:
            (project_dir / relative).mkdir(parents=True, exist_ok=True)
        _write_manifest(project_dir / "manifest.json", manifest)
    except Exception:
        if project_dir.exists():
            shutil.rmtree(project_dir)
        raise
    return {"status": "created", "project_directory": str(project_dir), "manifest_path": str(project_dir / "manifest.json"),
            "directories": [str(project_dir / item) for item in PROJECT_DIRECTORIES], "manifest": manifest}


def get_project(project_directory: str) -> dict[str, Any]:
    path = _manifest_path(project_directory)
    return {"project_directory": str(path.parent), "manifest_path": str(path), "manifest": _read_manifest(path)}


def update_model_specification(project_directory: str, specification: dict[str, Any],
                               forcefield: dict[str, Any] | None = None,
                               science_contract: dict[str, Any] | None = None,
                               geology_model: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(specification, dict):
        raise ValueError("specification must be an object")
    path = _manifest_path(project_directory)
    with _locked_manifest(path) as manifest:
        manifest.setdefault("model_specification", {}).update(specification)
        if forcefield is not None:
            if not isinstance(forcefield, dict):
                raise ValueError("forcefield must be an object")
            manifest.setdefault("forcefield", {}).update(forcefield)
        if science_contract is not None:
            if not isinstance(science_contract, dict):
                raise ValueError("science_contract must be an object")
            # The contract is replaced as one versioned unit; partial merging can
            # silently retain stale physical semantics from an older profile.
            manifest["science_contract"] = deepcopy(science_contract)
        if geology_model is not None:
            if not isinstance(geology_model, dict):
                raise ValueError("geology_model must be an object")
            manifest.setdefault("geology_model", {}).update(geology_model)
        manifest["project"]["updated_at"] = _now()
    return validate_project(str(path.parent))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def register_artifact(project_directory: str, artifact_path: str, role: str, source: str | None = None) -> dict[str, Any]:
    manifest_path = _manifest_path(project_directory)
    project_dir = manifest_path.parent
    artifact = _ensure_allowed(Path(artifact_path))
    if project_dir != artifact and project_dir not in artifact.parents:
        raise PermissionError("Artifacts must be stored inside the project directory")
    if not artifact.is_file():
        raise FileNotFoundError(f"Artifact not found: {artifact}")
    relative = artifact.relative_to(project_dir).as_posix()
    entry = {"path": relative, "role": role, "size_bytes": artifact.stat().st_size, "sha256": _sha256(artifact),
             "source": source, "registered_at": _now()}
    with _locked_manifest(manifest_path) as manifest:
        artifacts = manifest.setdefault("artifacts", [])
        artifacts[:] = [item for item in artifacts if item.get("path") != relative]
        artifacts.append(entry)
        manifest.setdefault("provenance", {}).setdefault("file_hashes", {})[relative] = entry["sha256"]
        manifest["project"]["updated_at"] = _now()
    return {"status": "registered", "artifact": entry, "manifest_path": str(manifest_path)}


def validate_project(project_directory: str) -> dict[str, Any]:
    manifest_path = _manifest_path(project_directory)
    project_dir = manifest_path.parent
    manifest = _read_manifest(manifest_path)
    errors: list[str] = []
    warnings: list[str] = []
    project = manifest.get("project", {})
    if project.get("status") not in PROJECT_STATUSES:
        errors.append("project.status is missing or invalid")
    spec = manifest.get("model_specification", {})
    if not isinstance(spec.get("composition"), list) or not spec.get("composition"):
        errors.append("model_specification.composition is required")
    temperature = spec.get("temperature_k")
    if isinstance(temperature, bool) or not isinstance(temperature, (int, float)) or temperature <= 0:
        errors.append("model_specification.temperature_k must be positive numeric metadata")
    periodic_axes = spec.get("periodic_axes")
    if (
        not isinstance(periodic_axes, list)
        or len(set(periodic_axes)) != len(periodic_axes)
        or any(axis not in {"x", "y", "z"} for axis in periodic_axes)
    ):
        errors.append("model_specification.periodic_axes must contain unique x/y/z values")
    if spec.get("pressure_mpa") is None:
        warnings.append("model_specification.pressure_mpa is not set")
    forcefield = manifest.get("forcefield", {})
    for key in ("name", "units", "charge_model", "mixing_rule"):
        if forcefield.get(key) in (None, ""):
            errors.append(f"forcefield.{key} is required before conversion")
    science = validate_science_contract(manifest)
    errors.extend(f"science_contract: {item}" for item in science["errors"])
    warnings.extend(science["warnings"])
    missing_dirs = [item for item in PROJECT_DIRECTORIES if not (project_dir / item).is_dir()]
    if missing_dirs:
        errors.append("Missing project directories: " + ", ".join(missing_dirs))
    artifact_results = []
    for entry in manifest.get("artifacts", []):
        path = project_dir / entry.get("path", "")
        exists = path.is_file()
        hash_matches = exists and entry.get("sha256") == _sha256(path)
        artifact_results.append({"path": entry.get("path"), "exists": exists, "hash_matches": hash_matches})
        if not exists or not hash_matches:
            errors.append(f"Artifact missing or changed: {entry.get('path')}")
    spec_ready = not any(item.startswith("model_specification.") for item in errors)
    forcefield_ready = not any(item.startswith("forcefield.") for item in errors)
    return {"status": "valid" if not errors else "incomplete", "project_directory": str(project_dir),
            "specification_ready": spec_ready, "forcefield_ready": forcefield_ready, "errors": errors,
            "warnings": warnings, "science_contract": science,
            "production_allowed": not errors and science["production_allowed"],
            "artifact_checks": artifact_results, "manifest": manifest}


def transition_project_status(
    project_directory: str,
    target_status: str,
    *,
    reason: str,
    evidence_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Move a project through the explicit lifecycle with an audited transition.

    Repeating the already-completed transition is idempotent.  Backward jumps,
    skipped stages, and transitions out of terminal states fail closed.
    """
    if target_status not in PROJECT_STATUSES:
        raise ValueError(f"Unknown target project status: {target_status}")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("reason must be a non-empty string")
    if evidence_ids is not None and (
        not isinstance(evidence_ids, list)
        or any(not isinstance(item, str) or not item.strip() for item in evidence_ids)
    ):
        raise ValueError("evidence_ids must be an array of non-empty strings")
    path = _manifest_path(project_directory)
    with _locked_manifest(path) as manifest:
        current = manifest["project"]["status"]
        if current == target_status:
            return {
                "status": target_status,
                "previous_status": current,
                "changed": False,
                "replayed": True,
                "manifest_path": str(path),
            }
        if target_status not in PROJECT_TRANSITIONS[current]:
            raise ValueError(f"Illegal project status transition: {current} -> {target_status}")
        if target_status == "production":
            science = validate_science_contract(manifest)
            if not science["production_allowed"]:
                reasons = science["errors"] or science["warnings"] or ["scientific contract is not production-ready"]
                raise PermissionError("Production transition blocked by science contract: " + "; ".join(reasons))
        record = {
            "from": current,
            "to": target_status,
            "reason": reason.strip(),
            "evidence_ids": list(dict.fromkeys(evidence_ids or [])),
            "recorded_at": _now(),
        }
        manifest["project"]["status"] = target_status
        manifest["project"]["updated_at"] = record["recorded_at"]
        manifest.setdefault("project_status_history", []).append(record)
    return {
        "status": target_status,
        "previous_status": current,
        "changed": True,
        "replayed": False,
        "transition": record,
        "manifest_path": str(path),
    }


def _record_quality_gate(
    project_directory: str,
    gate: str,
    status: str,
    evidence: dict[str, Any],
    *,
    source: str,
) -> dict[str, Any]:
    path = _manifest_path(project_directory)
    with _locked_manifest(path) as manifest:
        if gate not in manifest.get("quality_gates", {}):
            raise ValueError(f"Unknown quality gate: {gate}")
        if status not in QUALITY_GATE_STATUSES:
            raise ValueError(f"status must be one of: {sorted(QUALITY_GATE_STATUSES)}")
        previous = manifest["quality_gates"][gate]
        if previous == "pass" and status != "pass" and not source.startswith("validator:"):
            raise ValueError("A verified pass can only be changed by a trusted validator")
        manifest["quality_gates"][gate] = status
        record = {
            "previous_status": previous,
            "status": status,
            "recorded_at": _now(),
            "source": source,
            "evidence": evidence,
        }
        manifest.setdefault("quality_gate_evidence", {})[gate] = record
        manifest.setdefault("quality_gate_history", {}).setdefault(gate, []).append(record)
        manifest["project"]["updated_at"] = _now()
    return {"gate": gate, "previous_status": previous, "status": status, "source": source,
            "manifest_path": str(path)}


def set_quality_gate(project_directory: str, gate: str, status: str, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    """Record a non-passing gate decision supplied by an MCP caller.

    A caller may reset work to pending or record fail/blocked, but may not claim
    that a gate passed. Pass decisions must come from a trusted validator through
    ``_record_verified_quality_gate`` and carry the validator identity and result.
    """
    if status == "pass":
        raise PermissionError("Callers cannot set a quality gate to pass; run a trusted validator")
    if status not in CALLER_SETTABLE_GATE_STATUSES:
        raise ValueError(f"status must be one of: {sorted(CALLER_SETTABLE_GATE_STATUSES)}")
    if evidence is not None and not isinstance(evidence, dict):
        raise ValueError("evidence must be an object")
    return _record_quality_gate(
        project_directory, gate, status, evidence or {}, source="caller",
    )


def _record_verified_quality_gate(
    project_directory: str,
    gate: str,
    *,
    validator: str,
    passed: bool,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    """Record the result of a trusted in-process validator.

    This private entry point is deliberately not exposed as an MCP tool. Validators
    must provide a stable source name and structured evidence; the boolean result,
    rather than caller-provided status text, determines pass or fail.
    """
    if not isinstance(validator, str) or not validator.strip():
        raise ValueError("validator must be a non-empty trusted validator identifier")
    if not isinstance(passed, bool):
        raise ValueError("passed must be a boolean validator result")
    if not isinstance(evidence, dict) or not evidence:
        raise ValueError("verified gate decisions require non-empty structured evidence")
    verified_evidence = dict(evidence)
    verified_evidence["validator"] = validator.strip()
    verified_evidence["validator_result"] = passed
    return _record_quality_gate(
        project_directory, gate, "pass" if passed else "fail", verified_evidence,
        source=f"validator:{validator.strip()}",
    )
