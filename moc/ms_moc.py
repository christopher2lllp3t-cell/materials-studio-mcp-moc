from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Any


MOC_DIRECTORY = Path(__file__).resolve().parent
KNOWN_WORKSPACE_ROOT = Path(r"D:\分子动力学模拟")
_workspace_override = os.environ.get("MS_MOC_WORKSPACE_ROOT")
_workspace_candidate = MOC_DIRECTORY.parent
ROOT = (
    Path(_workspace_override).expanduser().resolve()
    if _workspace_override
    else _workspace_candidate
    if (_workspace_candidate / "ms_scripts").is_dir() and (_workspace_candidate / "ms_output").is_dir()
    else KNOWN_WORKSPACE_ROOT
)
MS_SCRIPTS = ROOT / "ms_scripts"
MS_OUTPUT = ROOT / "ms_output"
SYSTEM_DIR = Path(r"E:\MD_Workflow\systems\viny10H_paper_recipe_6nm")
DESKTOP_PACKAGE = Path(r"C:\Users\86130\Desktop\MS_cleanup_stage5c")
MS_ROOT = Path(r"D:\Program Files (x86)\BIOVIA\Materials Studio 23.1")
RUNNER = MS_ROOT / "etc" / "Scripting" / "bin" / "RunMatScript.bat"
MATSTUDIO = MS_ROOT / "bin" / "MatStudio.exe"
WINDOWS_ROOT = Path(os.environ.get("SystemRoot", r"C:\Windows"))
TASKLIST = WINDOWS_ROOT / "System32" / "tasklist.exe"
POWERSHELL = WINDOWS_ROOT / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
TEMP_ROOT = Path(r"E:\ms_mcp\ms_mcp_jobs\moc_interface")
SOURCE_MCP_ROOT = Path(r"E:\ms_mcp\ms_mcp_runtime\materials_studio_2023")
_mcp_override = os.environ.get("MS_MOC_MCP_ROOT")
_deployment_candidate = MOC_DIRECTORY.parent
MCP_ROOT = (
    Path(_mcp_override).expanduser().resolve()
    if _mcp_override
    else _deployment_candidate
    if (_deployment_candidate / "release-bundle.json").is_file() and (_deployment_candidate / ".venv" / "Scripts" / "python.exe").is_file()
    else SOURCE_MCP_ROOT
)
MCP_PYTHON = MCP_ROOT / ".venv" / "Scripts" / "python.exe"
MCP_BRIDGE = MOC_DIRECTORY / "ms_mcp_bridge.py"
RELEASE_MANIFEST = MCP_ROOT / "release-manifest.json"
SCIENCE_DIRECTORY_NAME = "07_mcp_materials_studio"
DESKTOP_LAUNCH_GRACE_SECONDS = 10.0

SAFE_DATA = SYSTEM_DIR / "stage5c_rigid_oil_ultraslow_extend.data"
SAFE_CAR = SYSTEM_DIR / "viny10H_ms_cleanup_stage5c.car"
SAFE_MDF = SYSTEM_DIR / "viny10H_ms_cleanup_stage5c.mdf"
GUARDED_DATA = SYSTEM_DIR / "stage5d_ms_guarded_targeted_cleanup.data"
GUARDED_CAR = SYSTEM_DIR / "viny10H_ms_guarded_cleanup_stage5d.car"
GUARDED_MDF = SYSTEM_DIR / "viny10H_ms_guarded_cleanup_stage5d.mdf"

FAILED_FILES = [
    SYSTEM_DIR / "stage5a_oil_water_restrained_min.data",
    SYSTEM_DIR / "stage5b_rigid_oil_water_damped.data",
    SYSTEM_DIR / "stage5d_ms_rigid_cleanup_1p20.data",
    SYSTEM_DIR / "stage5d_ms_trial_translate_cleanup.data",
]
ALLOWED_DOCUMENT_ROOTS = (
    ROOT,
    Path(r"E:\MD_Workflow"),
    Path(r"E:\codex文件放置"),
    Path(r"E:\ms_mcp"),
)
ALLOWED_DOCUMENT_SUFFIXES = {".xsd", ".xtd", ".stp", ".car", ".mdf", ".cif"}
SCRIPT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")


def load_release_identity() -> dict[str, Any]:
    """Read the release identity from the authoritative manifest only."""
    if not RELEASE_MANIFEST.is_file():
        return {"status": "missing", "manifest_path": str(RELEASE_MANIFEST), "version": None}
    try:
        data = json.loads(RELEASE_MANIFEST.read_text(encoding="utf-8"))
        release = data.get("release", {})
        version = release.get("version")
        if not isinstance(version, str) or not version:
            return {"status": "invalid", "manifest_path": str(RELEASE_MANIFEST), "version": None}
        return {"status": "ready", "manifest_path": str(RELEASE_MANIFEST), "version": version, "api_version": release.get("api_version")}
    except (OSError, json.JSONDecodeError) as exc:
        return {"status": "invalid", "manifest_path": str(RELEASE_MANIFEST), "version": None, "error": str(exc)}


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def resolve_science_root(value: Path | None = None) -> Path:
    """Resolve science evidence from an explicit path, environment, or known workspace."""
    if value is not None:
        return value.expanduser().resolve(strict=True)
    configured = os.environ.get("MS_MOC_SCIENCE_ROOT")
    if configured:
        return Path(configured).expanduser().resolve(strict=True)
    for candidate in (ROOT / SCIENCE_DIRECTORY_NAME, KNOWN_WORKSPACE_ROOT / SCIENCE_DIRECTORY_NAME):
        if candidate.is_dir():
            return candidate.resolve(strict=True)
    raise FileNotFoundError(
        "Science evidence root is unavailable; set MS_MOC_SCIENCE_ROOT or pass --science-root"
    )


def classify_g02_phases(phases: dict[str, str]) -> str:
    values = set(phases.values())
    if "failed" in values:
        return "cutoff_sensitivity_execution_failed"
    if "resume_required" in values:
        return "cutoff_sensitivity_resume_required"
    if "running" in values:
        return "cutoff_sensitivity_running"
    if values and values == {"complete"}:
        return "cutoff_sensitivity_complete_pending_analysis"
    if "complete" in values:
        return "cutoff_sensitivity_partially_complete"
    if values and values <= {"pending", "missing"}:
        return "cutoff_sensitivity_not_started"
    return "cutoff_sensitivity_state_unknown"


def classify_g04_th01_phases(phases: dict[str, str]) -> str:
    values = set(phases.values())
    if "failed" in values:
        return "triaxial_thermodynamic_gate_execution_failed"
    if "resume_required" in values:
        return "triaxial_thermodynamic_gate_resume_required"
    if "running" in values:
        return "triaxial_thermodynamic_gate_running"
    if values and values == {"complete"}:
        return "triaxial_thermodynamic_gate_complete_pending_analysis"
    if "complete" in values:
        return "triaxial_thermodynamic_gate_partially_complete"
    if values and values <= {"pending", "missing"}:
        return "triaxial_thermodynamic_gate_not_started"
    return "triaxial_thermodynamic_gate_state_unknown"


def max_lammps_thermo_step(log_paths: list[Path], *, field_count: int) -> int:
    maximum = 0
    for path in log_paths:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            fields = line.split()
            if len(fields) == field_count and fields[0].isdigit():
                maximum = max(maximum, int(fields[0]))
    return maximum


def validate_ch03_production_evidence(
    ch03_root: Path,
    contract_path: Path,
    required_input_names: tuple[str, ...],
) -> dict[str, Any]:
    """Validate the hash-bound chapter 3 production evidence, when present."""
    evidence_path = ch03_root / "ch03_production_evidence.json"
    result: dict[str, Any] = {
        "status": "missing",
        "path": str(evidence_path),
        "sha256": None,
        "production_released": False,
        "errors": [],
    }
    if not evidence_path.is_file():
        return result
    result["sha256"] = sha256_file(evidence_path)
    try:
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        result["status"] = "fail"
        result["errors"] = [f"production evidence is unreadable: {exc}"]
        return result

    errors: list[str] = []
    if evidence.get("schema_version") != 1 or evidence.get("contract_id") != "CH03-EXACT-01":
        errors.append("production evidence identity or schema differs")
    if evidence.get("status") != "production_pass" or evidence.get("production_released") is not True:
        errors.append("production evidence does not record a passing release decision")
    if not contract_path.is_file() or evidence.get("exact_contract_sha256") != sha256_file(contract_path):
        errors.append("production evidence is not bound to the current exact-reproduction contract")

    input_hashes = evidence.get("required_input_sha256")
    if not isinstance(input_hashes, dict) or set(input_hashes) != set(required_input_names):
        errors.append("production evidence input hash ledger is incomplete")
    else:
        for name in required_input_names:
            path = ch03_root / name
            if not path.is_file() or input_hashes.get(name) != sha256_file(path):
                errors.append(f"production input is missing or changed: {name}")

    engine = evidence.get("engine") or {}
    if engine.get("name") != "NAMD" or not str(engine.get("version", "")).strip():
        errors.append("NAMD engine name/version evidence is incomplete")
    if not re.fullmatch(r"[0-9A-Fa-f]{64}", str(engine.get("executable_sha256", ""))):
        errors.append("NAMD executable hash is missing or invalid")

    protocol = evidence.get("protocol") or {}
    expected_protocol = {
        "equilibration_ns": 2.0,
        "production_sampling_ns": 1.0,
        "sampling_blocks": 5,
        "timestep_fs": 1.0,
        "temperature_K": 323.0,
    }
    for key, expected in expected_protocol.items():
        if protocol.get(key) != expected:
            errors.append(f"production protocol differs for {key}")

    gates = evidence.get("gates") or {}
    required_gates = {
        "trajectory_complete",
        "numerically_stable",
        "fixed_walls_verified",
        "region_definition_verified",
        "five_block_statistics_complete",
        "pressure_mapping_verified",
        "provenance_complete",
    }
    for gate in sorted(required_gates):
        if gates.get(gate) is not True:
            errors.append(f"production gate did not pass: {gate}")

    artifact_roles: set[str] = set()
    artifacts = evidence.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        errors.append("production artifact ledger is missing")
    else:
        root_resolved = ch03_root.resolve()
        for index, item in enumerate(artifacts):
            if not isinstance(item, dict):
                errors.append(f"production artifact {index} is invalid")
                continue
            role = str(item.get("role", ""))
            relative = Path(str(item.get("path", "")))
            if relative.is_absolute() or ".." in relative.parts:
                errors.append(f"production artifact {index} path is unsafe")
                continue
            path = (ch03_root / relative).resolve()
            if not path.is_relative_to(root_resolved):
                errors.append(f"production artifact {index} escaped the chapter directory")
                continue
            if not path.is_file() or item.get("sha256") != sha256_file(path):
                errors.append(f"production artifact is missing or changed: {relative}")
            artifact_roles.add(role)
    required_roles = {
        "trajectory",
        "restart",
        "energy_log",
        "block_statistics",
        "region_definition",
        "provenance",
    }
    if not required_roles <= artifact_roles:
        errors.append("production artifact roles are incomplete")

    analysis = evidence.get("analysis_implementation") or {}
    analysis_relative = Path(str(analysis.get("path", "")))
    if analysis_relative.is_absolute() or ".." in analysis_relative.parts or not analysis_relative.parts:
        errors.append("production analysis implementation path is unsafe or missing")
    else:
        analysis_path = (ch03_root / analysis_relative).resolve()
        if not analysis_path.is_relative_to(ch03_root.resolve()):
            errors.append("production analysis implementation escaped the chapter directory")
        elif not analysis_path.is_file() or analysis.get("sha256") != sha256_file(analysis_path):
            errors.append("production analysis implementation is missing or changed")

    result["status"] = "pass" if not errors else "fail"
    result["production_released"] = not errors
    result["errors"] = sorted(set(errors))
    return result


def validate_ch03_refprop10_evidence(
    workspace_root: Path,
    ch03_root: Path,
    contract: dict[str, Any],
) -> dict[str, Any]:
    """Validate the preregistered, hash-bound REFPROP 10 pressure mapping."""
    record = ((contract.get("observables") or {}).get("pressure_mapping_evidence") or {})
    evidence_path = ch03_root / "ch03_refprop10_evidence.json"
    result: dict[str, Any] = {
        "status": "missing",
        "method_id": record.get("method_id"),
        "path": str(evidence_path),
        "sha256": None,
        "refprop_version": None,
        "refprop_root": None,
        "errors": [],
    }
    if not record or not evidence_path.is_file():
        return result

    errors: list[str] = []
    expected_record = {
        "status": "validated_refprop10_method",
        "method_id": "CH03-EOS-RP01",
        "backend": "REFPROP",
        "refprop_version": "10.0",
        "wrapper": "CoolProp 8.0.0",
        "licensed_refprop_confirmed": True,
    }
    for key, expected in expected_record.items():
        if record.get(key) != expected:
            errors.append(f"REFPROP contract record differs for {key}")

    controlled: dict[str, tuple[Path, str | None]] = {}
    for label, path_key, hash_key in (
        ("preregistration", "preregistration_path", "preregistration_sha256"),
        ("implementation", "implementation_path", "implementation_sha256"),
        ("evidence", "evidence_path", "evidence_sha256"),
    ):
        relative = Path(str(record.get(path_key, "")))
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            errors.append(f"REFPROP {label} path is unsafe or missing")
            continue
        path = (workspace_root / relative).resolve()
        if not path.is_relative_to(workspace_root.resolve()):
            errors.append(f"REFPROP {label} path escaped the workspace")
            continue
        expected_hash = record.get(hash_key)
        if not path.is_file() or sha256_file(path) != expected_hash:
            errors.append(f"REFPROP {label} is missing or changed")
        controlled[label] = (path, expected_hash)

    result["sha256"] = sha256_file(evidence_path)
    try:
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        result["status"] = "fail"
        result["errors"] = [f"REFPROP evidence is unreadable: {exc}"]
        return result

    expected_evidence = {
        "schema_version": 1,
        "method_id": "CH03-EOS-RP01",
        "status": "pass",
        "claim": "licensed_refprop_10_method_validation_via_pinned_coolprop_wrapper",
        "licensed_refprop_confirmed": True,
        "backend": "REFPROP",
        "components": ["CarbonDioxide", "Methane"],
        "refprop_version": "10.0",
        "coolprop_version": "8.0.0",
    }
    for key, expected in expected_evidence.items():
        if evidence.get(key) != expected:
            errors.append(f"REFPROP evidence differs for {key}")

    for label in ("preregistration", "implementation"):
        embedded = evidence.get(label) or {}
        expected_hash = controlled.get(label, (None, None))[1]
        if embedded.get("sha256") != expected_hash:
            errors.append(f"REFPROP evidence {label} hash differs")

    for ledger_name in ("refprop_artifacts", "wrapper_offline_wheels"):
        ledger = evidence.get(ledger_name)
        if not isinstance(ledger, list) or not ledger:
            errors.append(f"REFPROP evidence {ledger_name} ledger is missing")
            continue
        for index, item in enumerate(ledger):
            if not isinstance(item, dict) or item.get("hash_matches") is not True:
                errors.append(f"REFPROP evidence {ledger_name} item {index} did not pass")
                continue
            path = Path(str(item.get("path", "")))
            expected_hash = item.get("expected_sha256")
            actual_hash = item.get("actual_sha256")
            if (
                not path.is_absolute()
                or not path.is_file()
                or actual_hash != expected_hash
                or sha256_file(path) != expected_hash
            ):
                errors.append(f"REFPROP evidence {ledger_name} item {index} is missing or changed")

    validation = evidence.get("validation") or {}
    if (
        validation.get("status") != "pass"
        or validation.get("refprop_version") != "10.0"
        or validation.get("point_count") != 9
        or validation.get("errors") != []
    ):
        errors.append("REFPROP numerical validation summary did not pass")
    points = validation.get("points")
    if not isinstance(points, list) or len(points) != 9:
        errors.append("REFPROP numerical validation grid is incomplete")
    elif any(not all((point.get("checks") or {}).values()) for point in points):
        errors.append("REFPROP numerical validation contains a failed point check")
    monotonic = validation.get("pressure_monotonic_with_density")
    if not isinstance(monotonic, dict) or len(monotonic) != 3 or not all(monotonic.values()):
        errors.append("REFPROP pressure monotonicity checks did not pass")

    result["status"] = "pass" if not errors else "fail"
    result["refprop_version"] = evidence.get("refprop_version")
    result["refprop_root"] = evidence.get("refprop_root")
    result["errors"] = sorted(set(errors))
    return result


def validate_g02_analysis_lock(g02: Path, registration: dict[str, Any]) -> dict[str, Any]:
    lock_path = g02 / "g02_cs02_analysis_lock.json"
    errors: list[str] = []
    if not lock_path.is_file():
        return {"status": "fail", "path": str(lock_path), "errors": ["analysis lock is missing"]}
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"status": "fail", "path": str(lock_path), "errors": [f"analysis lock is unreadable: {exc}"]}
    registration_path = g02 / "g02_cs02_preregistration.json"
    if lock.get("gate_id") != "G02-CS-02" or lock.get("lock_type") != "retrospective_pre_window_provenance_lock":
        errors.append("analysis lock identity or type differs")
    if lock.get("registration_sha256") != sha256_file(registration_path):
        errors.append("analysis lock registration hash differs")
    for path_key, hash_key, expected_name in (
        ("analyzer_path", "analyzer_sha256", "analyze_g02_cs02.py"),
        ("analyzer_test_path", "analyzer_test_sha256", "test_g02_cs02_analyzer.py"),
    ):
        if lock.get(path_key) != expected_name:
            errors.append(f"analysis lock {path_key} differs")
            continue
        artifact = g02 / expected_name
        if not artifact.is_file() or sha256_file(artifact) != lock.get(hash_key):
            errors.append(f"analysis lock {expected_name} hash differs")
    evaluation_first = int(registration.get("protocol", {}).get("evaluation_steps", {}).get("first", 0))
    checkpoint_step = lock.get("pre_window_checkpoint_step")
    if lock.get("evaluation_window_first_step") != evaluation_first or not isinstance(checkpoint_step, int) or checkpoint_step >= evaluation_first:
        errors.append("analysis lock pre-window step boundary differs")
    expected_arms = {
        f'{arm.get("seed")}_{arm.get("cutoff_tag")}'
        for arm in registration.get("protocol", {}).get("arms", [])
    }
    snapshots = lock.get("arm_snapshots") if isinstance(lock.get("arm_snapshots"), list) else []
    snapshot_arms = {str(item.get("arm_key")) for item in snapshots if isinstance(item, dict)}
    if snapshot_arms != expected_arms or len(snapshots) != len(expected_arms):
        errors.append("analysis lock arm snapshot set differs")
    try:
        analyzer_time = datetime.fromisoformat(str(lock.get("analyzer_last_write_at_utc")).replace("Z", "+00:00"))
        test_time = datetime.fromisoformat(str(lock.get("analyzer_test_last_write_at_utc")).replace("Z", "+00:00"))
    except ValueError:
        analyzer_time = test_time = None
        errors.append("analysis lock analyzer timestamp is invalid")
    for snapshot in snapshots:
        if not isinstance(snapshot, dict):
            continue
        arm_key = str(snapshot.get("arm_key"))
        phase = snapshot.get("phase_at_lock")
        if phase == "pending":
            if any(snapshot.get(key) is not None for key in ("pre_window_checkpoint_path", "pre_window_checkpoint_sha256", "pre_window_checkpoint_created_at_utc")):
                errors.append(f"analysis lock pending arm {arm_key} has inconsistent checkpoint evidence")
            continue
        expected_path = f"g02_cs02_runs/{arm_key}/checkpoints/restart.{checkpoint_step}"
        if phase != "running" or snapshot.get("pre_window_checkpoint_path") != expected_path:
            errors.append(f"analysis lock arm {arm_key} phase or checkpoint path differs")
            continue
        checkpoint = g02 / expected_path
        if not checkpoint.is_file() or sha256_file(checkpoint) != snapshot.get("pre_window_checkpoint_sha256"):
            errors.append(f"analysis lock arm {arm_key} checkpoint hash differs")
            continue
        try:
            checkpoint_time = datetime.fromisoformat(str(snapshot.get("pre_window_checkpoint_created_at_utc")).replace("Z", "+00:00"))
        except ValueError:
            errors.append(f"analysis lock arm {arm_key} checkpoint timestamp is invalid")
            continue
        if analyzer_time is None or test_time is None or analyzer_time >= checkpoint_time or test_time >= checkpoint_time:
            errors.append(f"analysis lock arm {arm_key} does not prove pre-window analyzer provenance")
    return {
        "status": "pass" if not errors else "fail",
        "type": lock.get("lock_type"),
        "provenance_strength": "local_filesystem_timestamp_not_third_party_attested",
        "path": str(lock_path),
        "sha256": sha256_file(lock_path),
        "analyzer_sha256": lock.get("analyzer_sha256"),
        "created_at_utc": lock.get("lock_created_at_utc"),
        "errors": errors,
    }


def validate_g02_adjudication_lock(g02: Path, registration: dict[str, Any], analysis_lock: dict[str, Any]) -> dict[str, Any]:
    recovery_lock = g02 / "g02_cs02_recovery_adjudication_lock.json"
    lock_path = recovery_lock if recovery_lock.is_file() else g02 / "g02_cs02_adjudication_lock.json"
    errors: list[str] = []
    if not lock_path.is_file():
        return {"status": "fail", "path": str(lock_path), "errors": ["adjudication lock is missing"]}
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"status": "fail", "path": str(lock_path), "errors": [f"adjudication lock is unreadable: {exc}"]}
    gate_id = lock.get("gate_id")
    if gate_id not in {"G02-CS-02A", "G02-CS-02B"} or lock.get("original_states_must_be_preserved") is not True:
        errors.append("adjudication lock identity or preservation rule differs")
    if lock.get("registration_sha256") != sha256_file(g02 / "g02_cs02_preregistration.json"):
        errors.append("adjudication lock registration hash differs")
    if lock.get("frozen_numeric_analyzer_sha256") != analysis_lock.get("analyzer_sha256"):
        errors.append("adjudication lock numeric analyzer hash differs")
    if gate_id == "G02-CS-02A":
        runner_hash = next(
            (item.get("sha256") for item in registration.get("implementation_artifacts", []) if item.get("path") == "run_g02_cs02.py"),
            None,
        )
        if lock.get("original_runner_sha256") != runner_hash:
            errors.append("adjudication lock runner hash differs")
        expected_artifacts = (
            ("adjudicator_path", "adjudicator_sha256", "adjudicate_g02_cs02.py"),
            ("adjudicator_test_path", "adjudicator_test_sha256", "test_g02_cs02_adjudication.py"),
        )
    else:
        previous = g02 / str(lock.get("superseded_adjudication_lock_path", ""))
        if (
            not previous.is_file()
            or sha256_file(previous) != lock.get("superseded_adjudication_lock_sha256")
        ):
            errors.append("recovery adjudication lock does not preserve the prior lock")
        expected_artifacts = (
            ("adjudicator_path", "adjudicator_sha256", "adjudicate_g02_cs02_recovery.py"),
            ("adjudicator_test_path", "adjudicator_test_sha256", "test_g02_cs02_recovery_adjudication.py"),
        )
    for path_key, hash_key, expected_name in expected_artifacts:
        artifact = g02 / expected_name
        if lock.get(path_key) != expected_name or not artifact.is_file() or sha256_file(artifact) != lock.get(hash_key):
            errors.append(f"adjudication lock {expected_name} hash differs")
    return {
        "status": "pass" if not errors else "fail",
        "gate_id": gate_id,
        "path": str(lock_path),
        "sha256": sha256_file(lock_path),
        "adjudicator_sha256": lock.get("adjudicator_sha256"),
        "frozen_at_utc": lock.get("frozen_at_utc"),
        "scope": lock.get("scope"),
        "errors": errors,
    }


def validate_g04_analysis_lock(g04: Path, registration: dict[str, Any]) -> dict[str, Any]:
    lock_path = g04 / "g04_th01_analysis_lock.json"
    errors: list[str] = []
    if not lock_path.is_file():
        return {"status": "fail", "path": str(lock_path), "errors": ["analysis lock is missing"]}
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"status": "fail", "path": str(lock_path), "errors": [f"analysis lock is unreadable: {exc}"]}
    registration_path = g04 / "g04_th01_preregistration.json"
    if lock.get("gate_id") != "G04-TH-01":
        errors.append("analysis lock gate identity differs")
    if lock.get("registration_sha256") != sha256_file(registration_path):
        errors.append("analysis lock registration hash differs")
    for path_key, hash_key, expected_name in (
        ("analyzer_path", "analyzer_sha256", "analyze_g04_th01.py"),
        ("analyzer_test_path", "analyzer_test_sha256", "test_g04_th01_analysis.py"),
    ):
        if lock.get(path_key) != expected_name:
            errors.append(f"analysis lock {path_key} differs")
            continue
        artifact = g04 / expected_name
        if not artifact.is_file() or sha256_file(artifact) != lock.get(hash_key):
            errors.append(f"analysis lock {expected_name} hash differs")
    first_step = int(registration.get("protocol", {}).get("evaluation_window", {}).get("first_step", 0))
    expected_arms = {str(item.get("arm_key")) for item in registration.get("protocol", {}).get("arms", [])}
    snapshots = lock.get("arm_snapshots") if isinstance(lock.get("arm_snapshots"), list) else []
    snapshot_arms = {str(item.get("arm_key")) for item in snapshots if isinstance(item, dict)}
    if not first_step or lock.get("evaluation_window_first_step") != first_step:
        errors.append("analysis lock evaluation window differs")
    if snapshot_arms != expected_arms or len(snapshots) != len(expected_arms):
        errors.append("analysis lock arm snapshot set differs")
    for snapshot in snapshots:
        step = snapshot.get("max_observed_thermo_step") if isinstance(snapshot, dict) else None
        if not isinstance(step, int) or step >= first_step:
            errors.append("analysis lock was not frozen before every arm entered the evaluation window")
            break
    if lock.get("all_arms_before_evaluation_window") is not True:
        errors.append("analysis lock does not affirm pre-window freezing")
    return {
        "status": "pass" if not errors else "fail",
        "path": str(lock_path),
        "sha256": sha256_file(lock_path),
        "analyzer_sha256": lock.get("analyzer_sha256"),
        "frozen_at_utc": lock.get("frozen_at_utc"),
        "errors": errors,
    }


def require(path: Path, label: str) -> None:
    if not path.exists():
        raise FileNotFoundError(f"Missing {label}: {path}")


def is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def controlled_document(value: str) -> Path:
    path = Path(value).expanduser().resolve(strict=True)
    roots = [root.resolve() for root in ALLOWED_DOCUMENT_ROOTS if root.exists()]
    if not any(is_within(path, root) for root in roots):
        raise ValueError(f"Document is outside the allowed MOC roots: {path}")
    if path.suffix.lower() not in ALLOWED_DOCUMENT_SUFFIXES:
        raise ValueError(f"Unsupported Materials Studio document type: {path.suffix}")
    return path


def run_command(
    command: list[str], cwd: Path, *, timeout_seconds: int = 600, merge_stderr: bool = True,
) -> subprocess.CompletedProcess[str]:
    stderr: int | None = subprocess.STDOUT if merge_stderr else subprocess.PIPE
    try:
        return subprocess.run(
            command,
            cwd=str(cwd),
            text=True,
            stdout=subprocess.PIPE,
            stderr=stderr,
            check=False,
            timeout=timeout_seconds,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout if isinstance(exc.stdout, str) else ""
        return subprocess.CompletedProcess(command, 124, output + f"\nTimed out after {timeout_seconds} seconds\n", "")


def matstudio_pids() -> list[int]:
    result = run_command(
        [str(TASKLIST), "/FI", "IMAGENAME eq MatStudio.exe", "/FO", "CSV", "/NH"],
        MATSTUDIO.parent,
        timeout_seconds=10,
        merge_stderr=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Unable to query MatStudio processes: {result.stdout.strip()}")
    pids: list[int] = []
    for row in csv.reader(result.stdout.splitlines()):
        if len(row) >= 2 and row[0].lower() == "matstudio.exe":
            try:
                pids.append(int(row[1]))
            except ValueError:
                continue
    return sorted(set(pids))


def prepare_temp(project: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    temp = TEMP_ROOT / f"{project}_{stamp}"
    temp.mkdir(parents=True, exist_ok=False)
    return temp


def emit(data: dict[str, Any], json_mode: bool) -> None:
    if json_mode:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    print(f"# {data.get('title', 'MS MOC')}")
    for check in data.get("checks", []):
        print(f"{check['status']:7s} {check['label']}: {check['path']}")
    if "summary" in data:
        print(f"\nstatus={data['summary']['status']}")
    if "result" in data:
        print(json.dumps(data["result"], ensure_ascii=False, indent=2))


def collect_status() -> dict[str, Any]:
    paths = [
        ("project root", ROOT, True),
        ("ms_scripts", MS_SCRIPTS, True),
        ("ms_output", MS_OUTPUT, True),
        ("Materials Studio root", MS_ROOT, True),
        ("MatStudio", MATSTUDIO, True),
        ("RunMatScript", RUNNER, True),
        ("tasklist", TASKLIST, True),
        ("Windows PowerShell", POWERSHELL, True),
        ("MCP runtime", MCP_ROOT, True),
        ("MCP Python", MCP_PYTHON, True),
        ("MCP bridge", MCP_BRIDGE, True),
        ("safe LAMMPS data", SAFE_DATA, False),
        ("safe MS CAR", SAFE_CAR, False),
        ("safe MS MDF", SAFE_MDF, False),
        ("guarded cleanup data", GUARDED_DATA, False),
        ("guarded cleanup CAR", GUARDED_CAR, False),
        ("guarded cleanup MDF", GUARDED_MDF, False),
        ("desktop package", DESKTOP_PACKAGE, False),
    ]
    checks = [
        {"label": label, "path": str(path), "required": required, "status": "OK" if path.exists() else "MISSING"}
        for label, path, required in paths
    ]
    missing_required = [item["label"] for item in checks if item["required"] and item["status"] != "OK"]
    return {
        "schema_version": 2,
        "title": "MS MOC status",
        "release_identity": load_release_identity(),
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "summary": {"status": "ready" if not missing_required else "degraded", "missing_required": missing_required},
        "checks": checks,
        "failed_model_files": [
            {"path": str(path), "present": path.exists(), "must_not_use": True} for path in FAILED_FILES
        ],
        "allowed_document_roots": [str(path) for path in ALLOWED_DOCUMENT_ROOTS],
        "allowed_document_suffixes": sorted(ALLOWED_DOCUMENT_SUFFIXES),
    }


def run_matscript(
    script_name: str,
    args: list[str],
    input_files: dict[str, Path] | None = None,
    expected_outputs: list[str] | None = None,
    *,
    timeout_seconds: int = 600,
) -> dict[str, Any]:
    if SCRIPT_NAME.fullmatch(script_name) is None:
        raise ValueError("Invalid MaterialsScript name")
    require(RUNNER, "MaterialsScript runner")
    source = (MS_SCRIPTS / f"{script_name}.pl").resolve(strict=True)
    if source.parent != MS_SCRIPTS.resolve():
        raise ValueError("MaterialsScript escaped ms_scripts")
    staged_sources: dict[str, Path] = {}
    for alias, item in (input_files or {}).items():
        if SCRIPT_NAME.fullmatch(alias) is None:
            raise ValueError(f"Invalid input alias: {alias}")
        staged_sources[alias] = item.resolve(strict=True)
    reviewed_outputs: list[Path] = []
    for relative in expected_outputs or []:
        relative_path = Path(relative)
        if relative_path.is_absolute() or not relative_path.parts or ".." in relative_path.parts:
            raise ValueError(f"Expected output must be a safe relative path: {relative}")
        reviewed_outputs.append(relative_path)
    if not all(isinstance(item, str) and item.isascii() for item in args):
        raise ValueError("MaterialsScript arguments must be ASCII strings")
    temp = prepare_temp(script_name)
    shutil.copy2(source, temp / source.name)
    staged: dict[str, dict[str, Any]] = {}
    for alias, item in staged_sources.items():
        destination = temp / f"{alias}{item.suffix.lower()}"
        shutil.copy2(item, destination)
        staged[alias] = {"source": str(item), "staged": str(destination), "sha256": sha256_file(item)}
    args_file = temp / f"{script_name}_args.txt"
    args_file.write_text("\n".join(args) + "\n", encoding="ascii")
    result = run_command([str(RUNNER), "-project", script_name, "--"], temp, timeout_seconds=timeout_seconds)
    out_file = temp / f"{script_name}.pl.out"
    log_file = temp / f"{script_name}_Files" / "MatStudioLog.htm"
    script_output = out_file.read_text(encoding="latin-1", errors="replace") if out_file.exists() else ""
    log_text = log_file.read_text(encoding="latin-1", errors="replace") if log_file.exists() else ""
    log_ok = "Completion status: (OK)" in log_text and "Completion status: (FAIL)" not in log_text
    generated_outputs: list[dict[str, Any]] = []
    outputs_complete = True
    for relative in reviewed_outputs:
        path = temp / relative
        exists = path.is_file()
        outputs_complete = outputs_complete and exists
        generated_outputs.append({
            "relative_path": str(relative),
            "path": str(path),
            "exists": exists,
            "bytes": path.stat().st_size if exists else None,
            "sha256": sha256_file(path) if exists else None,
        })
    success = result.returncode == 0 and log_ok and outputs_complete
    audit = {
        "schema_version": 1,
        "operation": "run_matscript",
        "script": script_name,
        "success": success,
        "exit_code": result.returncode,
        "timed_out": result.returncode == 124,
        "temp_directory": str(temp),
        "staged_inputs": staged,
        "args_sha256": sha256_file(args_file),
        "script_output": script_output,
        "runner_output": result.stdout,
        "matstudio_log": str(log_file) if log_file.exists() else None,
        "generated_outputs": generated_outputs,
        "outputs_complete": outputs_complete,
    }
    (temp / "moc_audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    return audit


def run_bridge(command: list[str], *, timeout_seconds: int = 180) -> dict[str, Any]:
    require(MCP_PYTHON, "MCP Python")
    require(MCP_BRIDGE, "MCP bridge")
    result = run_command(
        [str(MCP_PYTHON), str(MCP_BRIDGE), *command], ROOT,
        timeout_seconds=timeout_seconds, merge_stderr=False,
    )
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"MCP bridge returned invalid JSON: {result.stdout[:500]}") from exc
    data["bridge_exit_code"] = result.returncode
    if result.stderr:
        data["bridge_stderr"] = result.stderr[-2000:]
    if result.returncode != 0:
        error = data.get("error")
        if isinstance(error, dict):
            raise RuntimeError(
                f"MCP bridge failed ({error.get('type', 'error')}): "
                f"{error.get('message', 'unknown error')}"
            )
        raise RuntimeError("MCP bridge failed without a structured error")
    return data


def run_mcp_json(command: list[str], *, timeout_seconds: int = 600) -> dict[str, Any]:
    require(MCP_PYTHON, "MCP Python")
    result = run_command(
        [str(MCP_PYTHON), *command], MCP_ROOT,
        timeout_seconds=timeout_seconds, merge_stderr=False,
    )
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"MCP command returned invalid JSON: {result.stdout[:500]}") from exc
    data["exit_code"] = result.returncode
    if result.stderr:
        data["stderr_tail"] = result.stderr[-2000:]
    return data


def validate_g01_report(report_path: Path) -> dict[str, Any]:
    report = report_path.resolve(strict=True)
    allowed_roots = [root.resolve() for root in (ROOT, MCP_ROOT) if root.exists()]
    if not any(is_within(report, root) for root in allowed_roots):
        raise ValueError(f"G01 report is outside the controlled roots: {report}")
    if report.name != "G01_V1_REPRODUCTION_REPORT.json":
        raise ValueError("G01 report must be the canonical G01_V1_REPRODUCTION_REPORT.json")
    data = json.loads(report.read_text(encoding="utf-8"))
    project = report.parents[1]
    manifest_path = project / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    errors: list[str] = []
    expected_report = {
        "status": "pass",
        "project_status": "validated",
        "fresh_run": True,
        "project_validation_status": "valid",
        "production_science_released": False,
    }
    for key, expected in expected_report.items():
        if data.get(key) != expected:
            errors.append(f"G01 report {key} must be {expected!r}")
    if data.get("conversion", {}).get("production_released") is not False:
        errors.append("G01 conversion must remain production_released=false")
    energy = data.get("energy_equivalence", {})
    if energy.get("pass") is not True:
        errors.append("G01 energy-equivalence gate did not pass")
    difference = energy.get("absolute_difference_kcal_mol")
    tolerance = energy.get("tolerance_kcal_mol")
    if not isinstance(difference, (int, float)) or not isinstance(tolerance, (int, float)) or difference > tolerance:
        errors.append("G01 energy difference exceeds or lacks its tolerance")
    gates = data.get("quality_gates", {})
    required_gates = {"structure", "forcefield", "lammps_preflight", "scientific_validation", "science_contract"}
    if {name for name in required_gates if gates.get(name) == "pass"} != required_gates:
        errors.append("G01 report does not contain all required passing quality gates")
    if manifest.get("project", {}).get("status") != "validated":
        errors.append("G01 project manifest is not validated")
    if manifest.get("quality_gates") != gates:
        errors.append("G01 report quality gates differ from the project manifest")

    artifact_checks: list[dict[str, Any]] = []
    for artifact in manifest.get("artifacts", []):
        relative = Path(str(artifact.get("path", "")))
        path = (project / relative).resolve()
        within_project = is_within(path, project.resolve())
        exists = within_project and path.is_file()
        actual = sha256_file(path) if exists else None
        matches = exists and actual.lower() == str(artifact.get("sha256", "")).lower()
        artifact_checks.append({"path": relative.as_posix(), "exists": exists, "hash_matches": matches})
        if not matches:
            errors.append(f"G01 artifact is missing or changed: {relative.as_posix()}")
    report_relative = report.relative_to(project).as_posix()
    report_artifact = next(
        (item for item in artifact_checks if item["path"] == report_relative), None
    )
    if report_artifact is None:
        errors.append("G01 final report is not registered in the project manifest")
    return {
        "schema_version": 1,
        "release_identity": load_release_identity(),
        "status": "pass" if not errors else "fail",
        "report_path": str(report),
        "report_sha256": sha256_file(report),
        "project_directory": str(project),
        "project_status": manifest.get("project", {}).get("status"),
        "artifact_count": len(artifact_checks),
        "artifact_checks": artifact_checks,
        "production_science_released": False,
        "errors": errors,
    }


def collect_doctor(*, run_version_probes: bool = True) -> dict[str, Any]:
    local = collect_status()
    errors: list[str] = []
    try:
        bridge = run_bridge(["status"] + (["--run-version-probes"] if run_version_probes else []))
    except Exception as exc:
        bridge = {"status": "error", "error": {"type": type(exc).__name__, "message": str(exc)}}
        errors.append(f"MCP bridge: {exc}")
    release_command = (
        ["-m", "materials_studio_mcp.release", "verify-deployment", "--root", str(MCP_ROOT)]
        if (MCP_ROOT / "release-bundle.json").is_file()
        else ["-m", "materials_studio_mcp.release", "verify", "--manifest", str(RELEASE_MANIFEST)]
    )
    release = (
        run_mcp_json(release_command)
        if RELEASE_MANIFEST.is_file()
        else {"status": "missing", "manifest_path": str(RELEASE_MANIFEST)}
    )
    if local["summary"]["status"] != "ready":
        errors.append("Required MOC paths are missing")
    expected_tool_count = release.get("public_tool_count")
    if bridge.get("status") != "ready" or bridge.get("tool_count") != expected_tool_count:
        errors.append(f"MCP bridge tool count does not match release manifest ({expected_tool_count})")
    if release.get("status") != "pass" or release.get("exit_code") != 0:
        errors.append("Release manifest is missing, stale, or invalid")
    return {
        "schema_version": 1,
        "release_identity": load_release_identity(),
        "status": "ready" if not errors else "degraded",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "local": local,
        "mcp": bridge,
        "release": release,
        "errors": errors,
    }


def collect_acceptance(g01_report: Path, *, timeout_seconds: int = 900) -> dict[str, Any]:
    doctor_result = collect_doctor(run_version_probes=True)
    science_result = collect_science_status()
    tests = run_command(
        [str(MCP_PYTHON), "-m", "unittest", "discover", "-s", "tests", "-q"],
        MCP_ROOT, timeout_seconds=timeout_seconds,
    )
    pip_check = run_command(
        [str(MCP_PYTHON), "-m", "pip", "check"], MCP_ROOT, timeout_seconds=120,
    )
    g01 = validate_g01_report(g01_report)
    tests_pass = tests.returncode == 0 and "OK" in tests.stdout
    pip_pass = pip_check.returncode == 0 and "No broken requirements found" in pip_check.stdout
    errors: list[str] = []
    if doctor_result["status"] != "ready":
        errors.append("MCP/MOC doctor check is degraded")
    if not tests_pass:
        errors.append("MCP regression suite failed")
    if not pip_pass:
        errors.append("Python dependency check failed")
    if g01["status"] != "pass":
        errors.append("G01 reproduction evidence failed verification")
    if science_result["status"] != "audited":
        errors.append("Model-specific science evidence is degraded")
    match = re.search(r"Ran\s+(\d+)\s+tests?", tests.stdout)
    return {
        "schema_version": 1,
        "status": "pass" if not errors else "fail",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "doctor": doctor_result,
        "regression": {
            "status": "pass" if tests_pass else "fail",
            "test_count": int(match.group(1)) if match else None,
            "exit_code": tests.returncode,
            "output_tail": tests.stdout[-4000:],
        },
        "pip_check": {
            "status": "pass" if pip_pass else "fail",
            "exit_code": pip_check.returncode,
            "output": pip_check.stdout.strip(),
        },
        "g01": g01,
        "model_science": science_result,
        "interpretation": "Acceptance pass covers the platform and G01 calibration; model_science.production_science_released controls G02/G04/G06 production release.",
        "production_science_released": False,
        "errors": errors,
    }


def collect_science_status(science_root: Path | None = None) -> dict[str, Any]:
    """Collect hash-checked model gates without claiming production release."""
    root = resolve_science_root(science_root)
    errors: list[str] = []
    models: dict[str, Any] = {}

    g02 = root / "golden_science" / "G02_nacl_brine"
    registration_path = g02 / "g02_cs02_preregistration.json"
    evidence_path = g02 / "g02_cs02_evidence.json"
    if not registration_path.is_file():
        errors.append("G02-CS-02 preregistration is missing")
        models["G02"] = {"status": "missing", "production_released": False}
    else:
        registration = json.loads(registration_path.read_text(encoding="utf-8"))
        g02_errors = []
        if registration.get("gate_id") != "G02-CS-02" or registration.get("registration_status") != "frozen_before_execution":
            g02_errors.append("G02-CS-02 preregistration identity/status is invalid")
        g02_analysis_lock = validate_g02_analysis_lock(g02, registration)
        if g02_analysis_lock["status"] != "pass":
            g02_errors.extend(f"G02-CS-02 {item}" for item in g02_analysis_lock["errors"])
        g02_adjudication_lock = validate_g02_adjudication_lock(g02, registration, g02_analysis_lock)
        if g02_adjudication_lock["status"] != "pass":
            g02_errors.extend(f"G02-CS-02A {item}" for item in g02_adjudication_lock["errors"])
        if evidence_path.is_file():
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            if evidence.get("gate_id") != "G02-CS-02":
                g02_errors.append("G02-CS-02 evidence identity is invalid")
            evidence_chain_valid = (
                evidence.get("status") in {"cutoff_sensitivity_pass", "cutoff_sensitivity_fail"}
                and evidence.get("production_released") is False
                and evidence.get("analysis_implementation_sha256") == g02_analysis_lock.get("analyzer_sha256")
                and evidence.get("frozen_contract_valid") is True
                and (evidence.get("batch_receipt") or {}).get("valid") is True
            )
            adjudication = evidence.get("execution_adjudication") or {}
            adjudication_path = g02 / str(adjudication.get("evidence_path", ""))
            adjudication_valid = (
                adjudication.get("gate_id") == g02_adjudication_lock.get("gate_id")
                and adjudication.get("adjudicator_sha256") == g02_adjudication_lock.get("adjudicator_sha256")
                and adjudication.get("original_states_preserved") is True
                and adjudication_path.is_file()
                and sha256_file(adjudication_path) == adjudication.get("evidence_sha256")
            )
            evidence_chain_valid = evidence_chain_valid and adjudication_valid
            if not evidence_chain_valid:
                g02_errors.append("G02-CS-02 terminal evidence chain is inconsistent")
            models["G02"] = {
                "status": evidence.get("status"),
                "selected_cutoff_A": evidence.get("selected_cutoff_A"),
                "evidence_path": str(evidence_path),
                "evidence_sha256": sha256_file(evidence_path),
                "analysis_lock": g02_analysis_lock,
                "execution_adjudication_lock": g02_adjudication_lock,
                "terminal_evidence_chain_valid": evidence_chain_valid,
                "production_released": evidence.get("production_released") is True,
                "errors": g02_errors,
            }
        else:
            state_root = g02 / "g02_cs02_runs"
            phases: dict[str, str] = {}
            checkpoints: dict[str, int] = {}
            if state_root.is_dir():
                for arm in registration.get("protocol", {}).get("arms", []):
                    key = f'{arm["seed"]}_{arm["cutoff_tag"]}'
                    state_path = state_root / key / "state.json"
                    if not state_path.is_file():
                        phases[key], checkpoints[key] = "missing", 0
                        continue
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                    phases[key] = str(state.get("phase", "unknown"))
                    checkpoint = state.get("last_valid_checkpoint") or {}
                    checkpoints[key] = int(checkpoint.get("step", 0))
            expected = len(registration.get("protocol", {}).get("arms", []))
            progress = sum(checkpoints.values()) / (expected * 200000) if expected else 0.0
            models["G02"] = {
                "status": classify_g02_phases(phases),
                "registration_path": str(registration_path),
                "registration_sha256": sha256_file(registration_path),
                "analysis_lock": g02_analysis_lock,
                "execution_adjudication_lock": g02_adjudication_lock,
                "remediation_status": "locked_execution_adjudication_pending_all_arms",
                "arm_phases": phases,
                "checkpoint_steps": checkpoints,
                "checkpoint_progress_fraction": progress,
                "production_released": False,
                "errors": g02_errors,
            }
        errors.extend(g02_errors)

    g04 = root / "production_gates" / "G04_charged_clay"
    manifest_path = g04 / "manifest.json"
    if not manifest_path.is_file():
        errors.append("G04 manifest is missing")
        models["G04"] = {"status": "missing", "production_released": False}
    else:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        gate_checks: dict[str, Any] = {}
        g04_errors = []
        for gate_name in (
            "parameter_family_gate",
            "lammps_static_gate",
            "runtime_smoke_gate",
            "pressure_tensor_diagnostic",
        ):
            gate = manifest.get(gate_name) or {}
            path = g04 / str(gate.get("evidence_path", ""))
            matches = path.is_file() and sha256_file(path) == gate.get("evidence_sha256")
            gate_checks[gate_name] = {"status": gate.get("status"), "hash_matches": matches}
            if not matches:
                g04_errors.append(f"G04 {gate_name} evidence is missing or changed")
        models["G04"] = {
            "status": manifest.get("status"),
            "manifest_path": str(manifest_path),
            "manifest_sha256": sha256_file(manifest_path),
            "gates": gate_checks,
            "fixed_box_pressure_warning": bool((manifest.get("runtime_smoke_gate") or {}).get("fixed_box_pressure_warning")),
            "z_only_relaxation_allowed": (manifest.get("pressure_tensor_diagnostic") or {}).get("z_only_relaxation_allowed"),
            "production_released": manifest.get("production_released") is True,
            "errors": g04_errors,
        }
        th_registration = g04 / "g04_th01_preregistration.json"
        th_lock = g04 / "g04_th01_preregistration.sha256"
        th_evidence = g04 / "g04_th01_evidence.json"
        if th_registration.is_file():
            registration = json.loads(th_registration.read_text(encoding="utf-8"))
            lock_matches = th_lock.is_file() and th_lock.read_text(encoding="ascii").strip() == sha256_file(th_registration)
            if not lock_matches:
                g04_errors.append("G04-TH-01 preregistration hash lock is missing or changed")
            analysis_lock = validate_g04_analysis_lock(g04, registration)
            if analysis_lock["status"] != "pass":
                g04_errors.extend(f"G04-TH-01 {item}" for item in analysis_lock["errors"])
            if th_evidence.is_file():
                th_data = json.loads(th_evidence.read_text(encoding="utf-8"))
                manifest_gate = manifest.get("thermodynamic_gate") or {}
                evidence_hash = sha256_file(th_evidence)
                evidence_valid = (
                    th_data.get("gate_id") == "G04-TH-01"
                    and th_data.get("status") in {"thermodynamic_equilibration_pass", "thermodynamic_equilibration_fail"}
                    and th_data.get("production_released") is False
                    and th_data.get("registration_sha256") == sha256_file(th_registration)
                    and th_data.get("analysis_implementation_sha256") == analysis_lock.get("analyzer_sha256")
                    and manifest_gate.get("status") == th_data.get("status")
                    and manifest_gate.get("evidence_sha256") == evidence_hash
                    and manifest.get("production_released") is False
                )
                if not evidence_valid:
                    g04_errors.append("G04-TH-01 terminal evidence chain is inconsistent")
                models["G04"]["thermodynamic_gate"] = {
                    "status": th_data.get("status"),
                    "evidence_path": str(th_evidence),
                    "evidence_sha256": evidence_hash,
                    "registration_hash_matches": lock_matches,
                    "analysis_lock": analysis_lock,
                    "terminal_evidence_chain_valid": evidence_valid,
                }
            else:
                phases: dict[str, str] = {}
                checkpoints: dict[str, int] = {}
                observed_steps: dict[str, int] = {}
                for arm in registration.get("protocol", {}).get("arms", []):
                    key = str(arm["arm_key"])
                    state_path = g04 / "g04_th01_runs" / key / "state.json"
                    if not state_path.is_file():
                        phases[key], checkpoints[key], observed_steps[key] = "missing", 0, 0
                        continue
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                    phases[key] = str(state.get("phase", "unknown"))
                    checkpoint = state.get("last_valid_checkpoint") or {}
                    checkpoints[key] = int(checkpoint.get("step", 0))
                    observed_steps[key] = max_lammps_thermo_step(
                        sorted(state_path.parent.glob("segment_*.log")), field_count=21
                    )
                target = int(registration.get("protocol", {}).get("target_step", 0))
                expected = len(registration.get("protocol", {}).get("arms", []))
                progress = sum(checkpoints.values()) / (expected * target) if expected and target else 0.0
                observed_progress = sum(observed_steps.values()) / (expected * target) if expected and target else 0.0
                models["G04"]["thermodynamic_gate"] = {
                    "status": classify_g04_th01_phases(phases),
                    "registration_path": str(th_registration),
                    "registration_sha256": sha256_file(th_registration),
                    "registration_hash_matches": lock_matches,
                    "analysis_lock": analysis_lock,
                    "arm_phases": phases,
                    "checkpoint_steps": checkpoints,
                    "checkpoint_progress_fraction": progress,
                    "observed_thermo_steps": observed_steps,
                    "observed_progress_fraction": observed_progress,
                }
        errors.extend(g04_errors)

    g06 = root / "production_gates" / "G06_quartz_nanopore"
    gap_path = g06 / "g06_release_gap_20260715.json"
    if not gap_path.is_file():
        errors.append("G06 release-gap evidence is missing")
        models["G06"] = {"status": "missing", "production_released": False}
    else:
        gap = json.loads(gap_path.read_text(encoding="utf-8"))
        g06_errors = []
        valid_g06_statuses = {
            "blocked_authentication_and_scientific_inputs_required",
            "blocked_scientific_inputs_required",
        }
        if gap.get("status") not in valid_g06_statuses:
            g06_errors.append("G06 release-gap status is unexpected")
        if gap.get("contract", {}).get("schema_errors") != 0:
            g06_errors.append("G06 contract contains schema errors")
        contract_record = gap.get("contract", {})
        contract_path = g06 / str(contract_record.get("path", ""))
        contract_hash_matches = (
            contract_path.is_file()
            and sha256_file(contract_path) == contract_record.get("sha256")
        )
        if not contract_hash_matches:
            g06_errors.append("G06 intake contract is missing or changed since release-gap audit")
        authenticated_methods_record = gap.get("authenticated_methods_discovery", {})
        authenticated_methods_path = g06 / str(authenticated_methods_record.get("path", ""))
        authenticated_methods_hash_matches = (
            authenticated_methods_path.is_file()
            and sha256_file(authenticated_methods_path) == authenticated_methods_record.get("sha256")
        )
        authentication_required = gap.get("authentication_boundary", {}).get(
            "required_for_exact_reproduction"
        )
        if not authentication_required and (
            authenticated_methods_record.get("status")
            != "accepted_as_hash_bound_main_method_evidence"
            or not authenticated_methods_hash_matches
        ):
            g06_errors.append(
                "G06 authenticated main-method evidence is missing or changed since release-gap audit"
            )
        models["G06"] = {
            "status": gap.get("status"),
            "blocker_count": contract_record.get("blocker_count"),
            "verified_literature_sources": contract_record.get("verified_literature_sources"),
            "verified_visual_reviews": contract_record.get("verified_visual_reviews"),
            "contract_hash_matches": contract_hash_matches,
            "authentication_required": authentication_required,
            "authenticated_full_text_dois": gap.get("authentication_boundary", {}).get("full_text_dois", []),
            "authenticated_methods_status": authenticated_methods_record.get("status"),
            "authenticated_methods_path": str(authenticated_methods_path),
            "authenticated_methods_sha256": authenticated_methods_record.get("sha256"),
            "authenticated_methods_hash_matches": authenticated_methods_hash_matches,
            "source_fact_count": len(gap.get("source_facts_required", [])),
            "research_choice_count": len(gap.get("research_choices_required_from_user", [])),
            "user_action": gap.get("authentication_boundary", {}).get("user_action"),
            "evidence_path": str(gap_path),
            "evidence_sha256": sha256_file(gap_path),
            "production_released": gap.get("construction_released") is True,
            "errors": g06_errors,
        }
        errors.extend(g06_errors)

    release_blockers: list[dict[str, Any]] = []
    g02_model = models.get("G02", {})
    g02_model["role"] = "qualification_fixture"
    g02_model["qualification_released"] = bool(
        g02_model.get("terminal_evidence_chain_valid")
        and g02_model.get("status") == "cutoff_sensitivity_pass"
        and not g02_model.get("errors")
    )
    if g02_model["qualification_released"]:
        g02_model["release_blockers"] = []
        g02_model["next_actions"] = []
    elif g02_model.get("terminal_evidence_chain_valid"):
        g02_blockers = [{
            "code": "G02_CUTOFF_SENSITIVITY_GATE_NOT_PASSED",
            "category": "scientific_gate",
            "detail": "Terminal cutoff-sensitivity evidence did not qualify a production cutoff.",
        }]
        g02_model["release_blockers"] = g02_blockers
        g02_model["next_actions"] = ["Review the frozen terminal evidence and preregister a new remediation gate without changing the completed thresholds."]
        release_blockers.extend({"model": "G02", **item} for item in g02_blockers)
    else:
        g02_blockers = [{
            "code": "G02_CUTOFF_TERMINAL_EVIDENCE_PENDING",
            "category": "execution",
            "detail": "All six cutoff arms and their locked adjudication must finish before the cutoff decision can be analyzed.",
        }]
        g02_model["release_blockers"] = g02_blockers
        g02_model["next_actions"] = ["Let all six arms reach terminal state and run the frozen execution adjudicator, which invokes the frozen statistical analyzer."]
        release_blockers.extend({"model": "G02", **item} for item in g02_blockers)

    g04_model = models.get("G04", {})
    g04_model["role"] = "qualification_fixture"
    thermodynamic = g04_model.get("thermodynamic_gate") or {}
    g04_model["qualification_released"] = bool(
        thermodynamic.get("status") == "thermodynamic_equilibration_pass"
        and thermodynamic.get("terminal_evidence_chain_valid")
        and not g04_model.get("errors")
    )
    if g04_model["qualification_released"]:
        g04_model["release_blockers"] = []
        g04_model["next_actions"] = []
    else:
        status = thermodynamic.get("status")
        code = (
            "G04_THERMODYNAMIC_GATE_NOT_PASSED"
            if status == "thermodynamic_equilibration_fail"
            else "G04_THERMODYNAMIC_TERMINAL_EVIDENCE_PENDING"
        )
        detail = (
            "The frozen triaxial thermodynamic qualification failed."
            if status == "thermodynamic_equilibration_fail"
            else "The triaxial qualification has not produced a valid passing terminal evidence chain."
        )
        g04_blockers = [{"code": code, "category": "scientific_gate", "detail": detail}]
        g04_model["release_blockers"] = g04_blockers
        g04_model["next_actions"] = ["Complete and analyze all three frozen G04-TH-01 arms, or preregister remediation if the terminal gate failed."]
        release_blockers.extend({"model": "G04", **item} for item in g04_blockers)

    g06_model = models.get("G06", {})
    g06_model["role"] = "qualification_fixture"
    g06_model["qualification_released"] = bool(
        g06_model.get("production_released")
        and g06_model.get("contract_hash_matches")
        and not g06_model.get("errors")
    )
    if not g06_model["qualification_released"]:
        g06_blockers: list[dict[str, str]] = []
        g06_actions: list[str] = []
        if g06_model.get("authentication_required"):
            g06_blockers.append({
                "code": "G06_AUTHENTICATED_METHODS_REQUIRED",
                "category": "user_authentication",
                "detail": "Exact reproduction still requires authenticated main-article methods or a verified author manuscript.",
            })
            g06_actions.append("Complete publisher or author-manuscript access in the visible browser; never send credentials or one-time codes to MOC.")
        if int(g06_model.get("blocker_count") or 0) > 0:
            g06_blockers.append({
                "code": "G06_CONSTRUCTION_CONTRACT_INCOMPLETE",
                "category": "scientific_inputs",
                "detail": f"The quartz nanopore intake contract still has {int(g06_model.get('blocker_count') or 0)} unresolved construction inputs.",
            })
            g06_actions.append("Extract the exact cleavage, termination, hydroxylation, force-field and fluid definitions, then freeze the completed intake contract before construction.")
        g06_model["release_blockers"] = g06_blockers
        g06_model["next_actions"] = g06_actions
        release_blockers.extend({"model": "G06", **item} for item in g06_blockers)

    workspace_root = root.parent
    ch03_root = workspace_root / "02_建模输入" / "ch03_竞争吸附"
    ch03_contract_path = ch03_root / "ch03_exact_reproduction_contract.json"
    ch03_contract = (
        json.loads(ch03_contract_path.read_text(encoding="utf-8"))
        if ch03_contract_path.is_file()
        else {}
    )
    contract_source = ch03_contract.get("source") or {}
    paper_path = workspace_root / str(contract_source.get("paper_path", ""))
    extracted_path = workspace_root / str(contract_source.get("extracted_text_path", ""))
    contract_sources_valid = (
        paper_path.is_file()
        and extracted_path.is_file()
        and sha256_file(paper_path) == contract_source.get("paper_sha256")
        and sha256_file(extracted_path) == contract_source.get("extracted_text_sha256")
    )
    solid_model = ch03_contract.get("solid_model") or {}
    paper_mineral = solid_model.get("paper_mineral") or {}
    fluid_models = ch03_contract.get("fluid_models") or {}
    candidate = paper_mineral.get("neutral_coordinate_candidate") or {}
    parameter_candidate = contract_source.get("local_mineral_parameter_candidate") or {}
    pressure_mapping_record = ((ch03_contract.get("observables") or {}).get("pressure_mapping_evidence") or {})
    contract_artifact_records = [
        (fluid_models.get("CH4") or {}).get("pdb_template") or {},
        (fluid_models.get("CO2") or {}).get("pdb_template") or {},
        {"path": candidate.get("gro_path"), "sha256": candidate.get("gro_sha256")},
        {"path": candidate.get("topology_path"), "sha256": candidate.get("topology_sha256")},
        {"path": parameter_candidate.get("path"), "sha256": parameter_candidate.get("sha256")},
        {"path": pressure_mapping_record.get("preregistration_path"), "sha256": pressure_mapping_record.get("preregistration_sha256")},
        {"path": pressure_mapping_record.get("implementation_path"), "sha256": pressure_mapping_record.get("implementation_sha256")},
        {"path": pressure_mapping_record.get("evidence_path"), "sha256": pressure_mapping_record.get("evidence_sha256")},
    ]
    contract_artifacts_valid = all(
        record.get("path")
        and (workspace_root / str(record["path"])).is_file()
        and sha256_file(workspace_root / str(record["path"])) == record.get("sha256")
        for record in contract_artifact_records
    )
    ch03_validation_path = ch03_root / "ms_pipeline_demo_fixed" / "ms_generated" / "ch03_paper_target_validation.json"
    ch03_validation = (
        json.loads(ch03_validation_path.read_text(encoding="utf-8"))
        if ch03_validation_path.is_file()
        else {}
    )
    required_ch03_names = (
        "graphene_mmt_pore_3nm.pdb",
        "graphene_mmt_pore_3nm.psf",
        "co2_trappe_rigid.pdb",
        "ch4_ua.pdb",
        "forcefield_ch03.prm",
    )
    ch03_inputs = {name: (ch03_root / name).is_file() for name in required_ch03_names}
    missing_ch03_inputs = [name for name, exists in ch03_inputs.items() if not exists]
    namd_value = os.environ.get("NAMD_EXE") or shutil.which("namd3") or shutil.which("namd2")
    namd_path = Path(namd_value).expanduser() if namd_value else None
    namd_available = bool(namd_path and namd_path.is_file())
    refprop_candidates = [
        Path(os.environ["RPPREFIX"]).expanduser() if os.environ.get("RPPREFIX") else None,
        Path(r"C:\Program Files (x86)\REFPROP"),
        Path(r"C:\Program Files\REFPROP"),
    ]
    refprop_path = next((path for path in refprop_candidates if path and path.is_dir()), None)
    refprop10_validation = validate_ch03_refprop10_evidence(
        workspace_root, ch03_root, ch03_contract
    )
    clay_is_surrogate = (ch03_validation.get("build_config") or {}).get("clay_is_surrogate") is True
    ch03_blockers: list[dict[str, str]] = []
    ch03_actions: list[str] = []
    if (
        ch03_contract.get("status") != "frozen_before_construction"
        or not contract_sources_valid
        or not contract_artifacts_valid
    ):
        ch03_blockers.append({
            "code": "CH03_EXACT_REPRODUCTION_CONTRACT_NOT_FROZEN",
            "category": "scientific_protocol",
            "detail": "The paper-derived exact-reproduction contract is still a blocked draft or its source hashes are invalid.",
        })
        ch03_actions.append("Populate every authoritative input, validate the source hashes, then freeze the exact-reproduction contract before construction.")
    geometry_audit = (
        (ch03_contract.get("solid_model") or {})
        .get("paper_mineral", {})
        .get("geometry_consistency_audit", {})
    )
    if geometry_audit.get("status") != "resolved_and_frozen":
        ch03_blockers.append({
            "code": "CH03_PAPER_GEOMETRY_DEFINITION_UNRESOLVED",
            "category": "scientific_inputs",
            "detail": "The paper does not establish whether the 30 x 18 unit-cell products are periodic box lengths while the smaller projected dimensions are atom-coordinate extents.",
        })
        ch03_actions.append("Freeze periodic box lengths, atom-coordinate extents and wall registry separately; do not silently rescale the mineral coordinates.")
    if parameter_candidate.get("status") != "validated_namd_exact_reproduction":
        ch03_blockers.append({
            "code": "CH03_MINERAL_FORCEFIELD_NAMD_MAPPING_REQUIRED",
            "category": "scientific_inputs",
            "detail": "A local Heinz/INTERFACE PCFF 9-6 mineral parameter candidate exists, but mineral charges and a lossless NAMD/cross-family mapping are not validated.",
        })
        ch03_actions.append("Recover the matching charged structure or source parameters, then validate 9-6 versus NAMD nonbonded semantics, charges, combining rules and exclusions before generating forcefield_ch03.prm.")
    if clay_is_surrogate or not ch03_validation:
        ch03_blockers.append({
            "code": "CH03_PAPER_EQUIVALENT_MINERAL_LAYER_REQUIRED",
            "category": "scientific_inputs",
            "detail": "The only assembled graphene-clay pore is surrogate mica. Exact reproduction requires the paper's neutral Si8Al4O20(OH)4 layer, with its MMT-versus-pyrophyllite-like identity warning preserved.",
        })
        ch03_actions.append("Acquire or deterministically build the paper-equivalent neutral mineral layer with provenance-locked Heinz parameters; keep a corrected charged-MMT study separate.")
    if missing_ch03_inputs:
        ch03_blockers.append({
            "code": "CH03_PRODUCTION_INPUT_FILES_MISSING",
            "category": "scientific_inputs",
            "detail": f"The chapter 3 NAMD workflow is missing {len(missing_ch03_inputs)} required PDB/PSF/parameter input files.",
        })
        ch03_actions.append("Generate and validate the pore PDB/PSF, CO2 and CH4 templates, and the complete chapter 3 parameter file.")
    if not namd_available:
        ch03_blockers.append({
            "code": "CH03_NAMD_RUNTIME_REQUIRED",
            "category": "user_authentication",
            "detail": "NAMD is not installed or configured; the official binary download requires user registration or login.",
        })
        ch03_actions.append("Register or sign in on the official NAMD download page, install NAMD 3.0.2 Win64-multicore, and set NAMD_EXE.")
    if refprop10_validation["status"] != "pass":
        ch03_blockers.append({
            "code": "CH03_REFPROP_OR_DECLARED_ALTERNATIVE_REQUIRED",
            "category": "licensed_dependency",
            "detail": "REFPROP 10 requires a licensed, version-checked and hash-bound pressure-mapping validation; directory presence alone is insufficient.",
        })
        ch03_actions.append("Validate the licensed REFPROP 10 API, pinned wrapper, property files and frozen CO2/CH4 density-to-pressure grid, or preregister a clearly labeled open alternative.")
    ch03_production_evidence = validate_ch03_production_evidence(
        ch03_root, ch03_contract_path, required_ch03_names
    )
    if not ch03_blockers and not ch03_production_evidence["production_released"]:
        evidence_code = (
            "CH03_PRODUCTION_EVIDENCE_REQUIRED"
            if ch03_production_evidence["status"] == "missing"
            else "CH03_PRODUCTION_EVIDENCE_INVALID"
        )
        ch03_blockers.append({
            "code": evidence_code,
            "category": "execution" if ch03_production_evidence["status"] == "missing" else "scientific_gate",
            "detail": "The complete 2 ns equilibration plus 1 ns production run has no valid hash-bound terminal evidence chain.",
        })
        ch03_actions.append("Execute the frozen NAMD protocol and validate the trajectory, fixed walls, regions, five-block statistics, pressure mapping and provenance evidence.")
    target_released = ch03_production_evidence["production_released"] and not ch03_blockers
    target_reproduction = {
        "target": "Chapter 3 CO2/CH4 competitive adsorption in a 3 nm graphene-MMT heterogeneous pore",
        "status": "released" if target_released else "blocked",
        "exact_contract_path": str(ch03_contract_path),
        "exact_contract_sha256": sha256_file(ch03_contract_path) if ch03_contract_path.is_file() else None,
        "exact_contract_status": ch03_contract.get("status"),
        "exact_contract_sources_valid": contract_sources_valid,
        "exact_contract_artifacts_valid": contract_artifacts_valid,
        "demo_validation_path": str(ch03_validation_path),
        "demo_validation_sha256": sha256_file(ch03_validation_path) if ch03_validation_path.is_file() else None,
        "clay_is_surrogate": clay_is_surrogate,
        "paper_mineral_label": "MMT",
        "paper_mineral_formula": "Si8Al4O20(OH)4",
        "mineral_identity_warning": "The paper formula is neutral and pyrophyllite-like; a charged montmorillonite model would be a separate corrected study, not an exact reproduction.",
        "geometry_consistency_audit": geometry_audit,
        "mineral_parameter_candidate": parameter_candidate,
        "required_inputs": ch03_inputs,
        "missing_input_count": len(missing_ch03_inputs),
        "namd_available": namd_available,
        "namd_path": str(namd_path) if namd_available else None,
        "refprop_installation_detected": refprop_path is not None,
        "refprop_available": refprop10_validation["status"] == "pass",
        "refprop_path": refprop10_validation.get("refprop_root") if refprop10_validation["status"] == "pass" else (str(refprop_path) if refprop_path else None),
        "refprop_validation": refprop10_validation,
        "production_evidence": ch03_production_evidence,
        "release_blockers": ch03_blockers,
        "next_actions": ch03_actions,
        "production_released": target_released,
    }
    release_blockers.extend({"model": "CH03", **item} for item in ch03_blockers)

    qualification_suite_released = bool(models) and all(
        item.get("qualification_released") is True for item in models.values()
    )
    released = qualification_suite_released and target_reproduction["production_released"] is True
    return {
        "schema_version": 1,
        "status": "audited" if not errors else "degraded",
        "release_identity": load_release_identity(),
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "science_root": str(root),
        "models": models,
        "qualification_suite_released": qualification_suite_released,
        "target_reproduction": target_reproduction,
        "production_science_released": released,
        "release_readiness": {
            "scope": "G02/G04/G06 qualification models and chapter 3 target reproduction",
            "platform_acceptance_is_separate": True,
            "blocker_count": len(release_blockers),
            "requires_user_action": any(item["category"] in {"user_authentication", "licensed_dependency"} for item in release_blockers),
            "blockers": release_blockers,
        },
        "interpretation": "G02/G04/G06 are qualification fixtures. Platform acceptance and fixture qualification do not replace the chapter 3 production evidence chain.",
        "errors": errors,
    }


def status(args: argparse.Namespace) -> int:
    data = collect_status()
    emit(data, args.json)
    return 0 if data["summary"]["status"] == "ready" else 1


def doctor(args: argparse.Namespace) -> int:
    data = collect_doctor(run_version_probes=not args.skip_version_probes)
    emit({"title": "MCP/MOC doctor", "result": data}, args.json)
    return 0 if data["status"] == "ready" else 1


def acceptance(args: argparse.Namespace) -> int:
    data = collect_acceptance(Path(args.g01_report), timeout_seconds=args.timeout_seconds)
    emit({"title": "MCP/MOC v1 acceptance", "result": data}, args.json)
    return 0 if data["status"] == "pass" else 1


def science_status(args: argparse.Namespace) -> int:
    data = collect_science_status(Path(args.science_root) if args.science_root else None)
    emit({"title": "MCP/MOC model science status", "result": data}, args.json)
    return 0 if data["status"] == "audited" else 1


def mcp_status(args: argparse.Namespace) -> int:
    data = run_bridge(["status"] + (["--run-version-probes"] if args.run_version_probes else []))
    emit({"title": "MCP status from MOC", "result": data}, args.json)
    required = data.get("required_bridge_tools", {})
    return 0 if data.get("status") == "ready" and all(required.values()) else 1


def mcp_tools(args: argparse.Namespace) -> int:
    data = run_bridge(["list-tools"])
    emit({"title": "MCP tools from MOC", "result": data}, args.json)
    return 0


def assess_nanopore(args: argparse.Namespace) -> int:
    contract = Path(args.contract).resolve(strict=True)
    data = run_bridge(["assess-nanopore", str(contract)])
    emit({"title": "Nanopore contract assessment", "result": data}, args.json)
    return 0


def list_scripts(args: argparse.Namespace) -> int:
    data = {
        "schema_version": 1,
        "title": "Available reviewed MS scripts",
        "powershell_wrappers": [rel(path) for path in sorted(MS_SCRIPTS.glob("run_*.ps1"))],
        "materialsscript_files": [rel(path) for path in sorted(MS_SCRIPTS.glob("*.pl"))],
    }
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print("# Available MS wrapper scripts")
        for path in data["powershell_wrappers"]:
            print(f"- {path}")
        print("\n# Available MaterialsScript files")
        for path in data["materialsscript_files"]:
            print(f"- {path}")
    return 0


def verify_xsd(args: argparse.Namespace) -> int:
    xsd = controlled_document(args.xsd)
    audit = run_matscript(
        "import_xsd_test", ["input_000.xsd"], {"input_000": xsd},
        timeout_seconds=args.timeout_seconds,
    )
    emit({"title": "MOC XSD verification", "result": audit}, args.json)
    return 0 if audit["success"] else 1


def export_xsd(args: argparse.Namespace) -> int:
    xsd = controlled_document(args.xsd)
    output_root = args.output_root or xsd.stem
    if SCRIPT_NAME.fullmatch(output_root) is None:
        raise ValueError("output_root must be a safe basename")
    audit = run_matscript(
        "export_xsd_to_car_mdf",
        ["input=input_000.xsd", f"output_root={output_root}"],
        {"input_000": xsd},
        [
            f"export_xsd_to_car_mdf_Files/Documents/{output_root}.car",
            f"export_xsd_to_car_mdf_Files/Documents/{output_root}.mdf",
        ],
        timeout_seconds=args.timeout_seconds,
    )
    emit({"title": "MOC XSD export", "result": audit}, args.json)
    return 0 if audit["success"] else 1


def launch(args: argparse.Namespace) -> int:
    require(MATSTUDIO, "MatStudio executable")
    document = controlled_document(args.document) if args.document else None
    command = [str(MATSTUDIO)] + ([str(document)] if document else [])
    data: dict[str, Any] = {
        "schema_version": 1,
        "operation": "launch",
        "status": "dry_run" if args.dry_run else "launched",
        "executable": str(MATSTUDIO),
        "document": str(document) if document else None,
        "document_sha256": sha256_file(document) if document and document.is_file() else None,
    }
    if not args.dry_run:
        desktop_pids_before = matstudio_pids()
        process = subprocess.Popen(
            command,
            cwd=str(MATSTUDIO.parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            close_fds=True,
        )
        time.sleep(DESKTOP_LAUNCH_GRACE_SECONDS)
        exit_code = process.poll()
        desktop_pids = matstudio_pids()
        if not desktop_pids_before and desktop_pids:
            time.sleep(DESKTOP_LAUNCH_GRACE_SECONDS)
            exit_code = process.poll()
            desktop_pids = matstudio_pids()
        if not desktop_pids:
            raise RuntimeError(f"MatStudio exited during launch with code {exit_code}")
        dispatcher_active = process.pid in desktop_pids
        stable_desktop_pids = sorted(set(desktop_pids_before) & set(desktop_pids))
        primary_pid = stable_desktop_pids[0] if stable_desktop_pids else desktop_pids[0]
        data.update({
            "pid": primary_pid,
            "dispatcher_pid": process.pid,
            "dispatcher_active": dispatcher_active,
            "desktop_pids": desktop_pids,
            "stable_desktop_pids": stable_desktop_pids,
            "launch_mode": (
                "requested_with_running_desktop" if desktop_pids_before
                else "cold_start_direct" if dispatcher_active
                else "cold_start_forwarded"
            ),
        })
    emit({"title": "MOC desktop launch", "result": data}, args.json)
    return 0


def diagnose_safe(args: argparse.Namespace) -> int:
    require(SAFE_DATA, "safe data")
    check = SYSTEM_DIR / "ms_moc_safe_check.md"
    result = run_command(
        [sys.executable, "check_structure_geometry.py", SAFE_DATA.name, check.name],
        SYSTEM_DIR, timeout_seconds=args.timeout_seconds,
    )
    data = {
        "schema_version": 1,
        "operation": "diagnose_safe",
        "exit_code": result.returncode,
        "output": result.stdout,
        "check_path": str(check) if check.exists() else None,
    }
    emit({"title": "MOC safe-model diagnosis", "result": data}, args.json)
    return result.returncode


def package_cleanup(args: argparse.Namespace) -> int:
    DESKTOP_PACKAGE.mkdir(parents=True, exist_ok=True)
    wanted = [
        SAFE_CAR, SAFE_MDF, GUARDED_CAR, GUARDED_MDF, GUARDED_DATA,
        SYSTEM_DIR / "viny10H_ms_guarded_cleanup_stage5d_diagnosis.md",
        SYSTEM_DIR / "stage5d_ms_guarded_targeted_cleanup_check.md",
        SYSTEM_DIR / "ms_cleanup_diagnosis_stage5c.md",
        SYSTEM_DIR / "MS_manual_cleanup_plan.md",
        SYSTEM_DIR / "relaxation_status.md",
    ]
    copied: list[dict[str, Any]] = []
    for path in wanted:
        require(path, "package file")
        destination = DESKTOP_PACKAGE / path.name
        shutil.copy2(path, destination)
        copied.append({"path": str(destination), "bytes": destination.stat().st_size, "sha256": sha256_file(destination)})
    emit({"title": "MOC cleanup package", "result": {"status": "updated", "directory": str(DESKTOP_PACKAGE), "files": copied}}, args.json)
    return 0


def run_wrapper(args: argparse.Namespace) -> int:
    name = args.script if args.script.lower().endswith(".ps1") else f"{args.script}.ps1"
    if SCRIPT_NAME.fullmatch(name) is None or not name.lower().startswith("run_"):
        raise ValueError("Only basename-only reviewed run_*.ps1 wrappers are allowed")
    wrapper = (MS_SCRIPTS / name).resolve(strict=True)
    if wrapper.parent != MS_SCRIPTS.resolve():
        raise ValueError("PowerShell wrapper escaped ms_scripts")
    require(POWERSHELL, "Windows PowerShell")
    command = [str(POWERSHELL), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(wrapper)]
    command.extend(args.wrapper_args or [])
    result = run_command(command, ROOT, timeout_seconds=args.timeout_seconds)
    data = {"status": "pass" if result.returncode == 0 else "fail", "exit_code": result.returncode, "output": result.stdout}
    emit({"title": "MOC reviewed wrapper", "result": data}, args.json)
    return result.returncode


def add_common_output_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Materials Studio MOC control interface for this workstation.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_status = sub.add_parser("status")
    add_common_output_flags(p_status)
    p_status.set_defaults(func=status)

    p_doctor = sub.add_parser("doctor")
    p_doctor.add_argument("--skip-version-probes", action="store_true")
    add_common_output_flags(p_doctor)
    p_doctor.set_defaults(func=doctor)

    p_acceptance = sub.add_parser("acceptance")
    p_acceptance.add_argument("--g01-report", required=True)
    p_acceptance.add_argument("--timeout-seconds", type=int, default=900)
    add_common_output_flags(p_acceptance)
    p_acceptance.set_defaults(func=acceptance)

    p_science = sub.add_parser("science-status")
    p_science.add_argument("--science-root")
    add_common_output_flags(p_science)
    p_science.set_defaults(func=science_status)

    p_mcp_status = sub.add_parser("mcp-status")
    p_mcp_status.add_argument("--run-version-probes", action="store_true")
    add_common_output_flags(p_mcp_status)
    p_mcp_status.set_defaults(func=mcp_status)

    p_mcp_tools = sub.add_parser("mcp-tools")
    add_common_output_flags(p_mcp_tools)
    p_mcp_tools.set_defaults(func=mcp_tools)

    p_assess = sub.add_parser("assess-nanopore")
    p_assess.add_argument("contract")
    add_common_output_flags(p_assess)
    p_assess.set_defaults(func=assess_nanopore)

    p_list = sub.add_parser("list-scripts")
    add_common_output_flags(p_list)
    p_list.set_defaults(func=list_scripts)

    for name in ("verify-xsd", "verify-model"):
        p_verify = sub.add_parser(name)
        p_verify.add_argument("xsd")
        p_verify.add_argument("--timeout-seconds", type=int, default=600)
        add_common_output_flags(p_verify)
        p_verify.set_defaults(func=verify_xsd)

    p_export = sub.add_parser("export-xsd")
    p_export.add_argument("xsd")
    p_export.add_argument("--output-root")
    p_export.add_argument("--timeout-seconds", type=int, default=600)
    add_common_output_flags(p_export)
    p_export.set_defaults(func=export_xsd)

    p_launch = sub.add_parser("launch")
    p_launch.add_argument("document", nargs="?")
    p_launch.add_argument("--dry-run", action="store_true")
    add_common_output_flags(p_launch)
    p_launch.set_defaults(func=launch)

    p_diagnose = sub.add_parser("diagnose-safe")
    p_diagnose.add_argument("--timeout-seconds", type=int, default=300)
    add_common_output_flags(p_diagnose)
    p_diagnose.set_defaults(func=diagnose_safe)

    p_package = sub.add_parser("package-cleanup")
    add_common_output_flags(p_package)
    p_package.set_defaults(func=package_cleanup)

    p_run = sub.add_parser("run-wrapper")
    p_run.add_argument("script")
    p_run.add_argument("--timeout-seconds", type=int, default=1800)
    add_common_output_flags(p_run)
    p_run.add_argument("wrapper_args", nargs=argparse.REMAINDER)
    p_run.set_defaults(func=run_wrapper)

    args = parser.parse_args()
    try:
        return args.func(args)
    except Exception as exc:
        print(json.dumps({
            "schema_version": 1,
            "status": "error",
            "error": {"type": type(exc).__name__, "message": str(exc)},
        }, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
