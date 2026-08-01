from __future__ import annotations

"""The bounded, qualification-only G01 MS -> LAMMPS -> VMD vertical path.

This module deliberately accepts one reviewed fixture and one reviewed PCFF
parameter pair.  It is an orchestration layer, not a new forcefield or model
generator.  The real path is never entered without an exact confirmation.
"""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any

from .confirmation import confirmation_manager
from .project_manager import (
    _record_verified_quality_gate,
    get_project,
    initialize_project,
    register_artifact,
    set_quality_gate,
    transition_project_status,
    update_model_specification,
)
from .project_manifest_v2 import (
    new_manifest_v2,
    register_artifact_v2,
    set_target_artifact_v2,
    transition_project_state_v2,
    validate_manifest_v2,
)
from .structure_preflight import inspect_lammps_data, inspect_structure_preflight
from .vmd_validation import validate_vmd_text_trajectory
from .pipeline_config import (
    approved_executable,
    discover_project_root,
    load_pipeline_config,
    resolve_workspace_path,
)
from .version_source import release_identity


_ROOT = discover_project_root(__file__)
_PROFILE_PATH = _ROOT / "config" / "qualification-profiles.json"
_V2_MANIFEST_NAME = "project-manifest.v2.json"
_STATE_NAME = "qualification-workflow-state.json"
_REPORT_NAME = "G01_VERTICAL_QUALIFICATION_REPORT.json"
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().lower()


def _hash_json(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{__import__('os').getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    temporary.replace(path)


def _profile() -> dict[str, Any]:
    try:
        data = json.loads(_PROFILE_PATH.read_text(encoding="utf-8"))
        profile = data["profiles"]["g01_pcff_vertical_v1"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Reviewed G01 qualification profile is unreadable: {_PROFILE_PATH}") from exc
    return deepcopy(profile)


def _safe_project_id(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip()).strip("._-")[:80]
    if not safe:
        raise ValueError("project_id must contain at least one letter or number")
    return safe


def _audit_input(path: Path, profile: dict[str, Any]) -> dict[str, Any]:
    digest = _sha256(path)
    if digest != profile["input_xsd_sha256"]:
        raise ValueError("The vertical qualification tool accepts only the hash-bound G01 XSD fixture")
    audit = inspect_structure_preflight(str(path))
    expected = {
        "atom_count": profile["expected_atom_count"],
        "elements": profile["expected_elements"],
        "bond_count": profile["expected_bond_count"],
        "bond_types": profile["expected_bond_types"],
        "periodic_axes": profile["periodic_axes"],
        "formal_charge": profile["expected_formal_charge"],
    }
    # Missing ForcefieldType is the sole accepted pre-preparation deficiency.
    # It must be repaired and fully audited at steps 4-5.
    errors = [
        item for item in audit.get("errors", [])
        if "ForcefieldType" not in str(item)
    ]
    for key in ("atom_count", "elements", "bond_count", "bond_types", "formal_charge"):
        actual = audit.get(key)
        expected_value = expected[key]
        if key == "formal_charge":
            matches = isinstance(actual, (int, float)) and abs(float(actual) - float(expected_value)) <= 1e-6
        else:
            matches = actual == expected_value
        if not matches:
            errors.append(f"G01 input {key} differs from reviewed fixture: {actual!r} != {expected_value!r}")
    return {
        "status": "pass" if not errors else "fail",
        "input_sha256": digest,
        "audit": audit,
        "expected": expected,
        "periodic": {"periodic_axes": list(profile["periodic_axes"]), "is_periodic": bool(profile["periodic_axes"])},
        "forcefield_typing_deferred_to_preparation": bool(audit.get("missing_forcefield_type_count")),
        "errors": errors,
    }


def _require_hash(value: str, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be a 64-character hexadecimal SHA-256")
    return value.lower()


def _nested_confirmation(tool_name: str, parameters: dict[str, Any], ttl_seconds: int) -> str:
    return confirmation_manager.issue(tool_name, parameters, ttl_seconds)["confirmation_token"]


def _checked_call(tool_name: str, parameters: dict[str, Any], *, ttl_seconds: int) -> dict[str, Any]:
    """Call an existing governed tool under the workflow's single approval."""
    from . import server

    call_parameters = dict(parameters)
    call_parameters["confirmation_token"] = _nested_confirmation(tool_name, parameters, ttl_seconds)
    result = getattr(server, tool_name)(**call_parameters)
    if not isinstance(result, dict) or result.get("ok") is not True:
        error = result.get("error") if isinstance(result, dict) else result
        raise RuntimeError(f"{tool_name} failed: {error}")
    return result.get("data") if isinstance(result.get("data"), dict) else result


def _stage_receipt(project: Path, name: str, payload: dict[str, Any]) -> Path:
    path = project / "reports" / "g01_vertical" / f"{name}.json"
    _write_json(path, payload)
    return path


def _advance_v1_status(project: Path, target: str) -> None:
    """Keep the legacy project manifest aligned without entering production."""
    current = get_project(str(project))["manifest"]["project"]["status"]
    if current == target:
        return
    order = {"draft": 0, "specified": 1, "modelled": 2, "converted": 3, "preflight_passed": 4}
    if order.get(current, -1) >= order.get(target, 99):
        return
    transition_project_status(str(project), target, reason=f"G01 vertical qualification reached {target}")


class _V2Ledger:
    def __init__(self, project: Path, project_id: str) -> None:
        self.project = project
        self.path = project / _V2_MANIFEST_NAME
        if self.path.is_file():
            self.manifest = json.loads(self.path.read_text(encoding="utf-8"))
            validate_manifest_v2(self.manifest)
        else:
            self.manifest = new_manifest_v2(project_id, "G01 PCFF MS-LAMMPS-VMD qualification", model_role="calibration")
            self._save()

    def _save(self) -> None:
        validate_manifest_v2(self.manifest)
        _write_json(self.path, self.manifest)

    def artifact(self, artifact_id: str, artifact_type: str, path: Path, parents: list[str], *, status: str = "VERIFIED", metadata: dict[str, Any] | None = None) -> str:
        relative = path.relative_to(self.project).as_posix()
        item = {
            "artifact_id": artifact_id,
            "artifact_type": artifact_type,
            "path": relative,
            "sha256": _sha256(path),
            "parent_artifact_ids": parents,
            "created_by": "materials_studio_mcp.md_g01_qualification_vertical",
            "tool_version": "qualification-workflow-v1",
            "created_at": _now(),
            "status": status,
        }
        if metadata is not None:
            item["metadata"] = metadata
        self.manifest = register_artifact_v2(self.manifest, item)
        self._save()
        return artifact_id

    def evidence(self, evidence_id: str, gate: str, path: Path, parents: list[str], *, kind: str, result: str = "PASS", status: str = "VERIFIED") -> str:
        metadata = {
            "evidence_kind": kind,
            "evidence_scope": "calibration_model",
            "model_role": "calibration",
            "gate": gate,
            "result": result,
            "subject_artifact_ids": list(parents),
        }
        return self.artifact(evidence_id, "evidence_receipt", path, parents, status=status, metadata=metadata)

    def state(self, target: str, evidence_ids: list[str], *, manual: bool = False) -> None:
        self.manifest = transition_project_state_v2(
            self.manifest, target, evidence_ids=evidence_ids,
            authorized_by="qualification-workflow" if manual else "validator",
            manual_authorization=manual,
        )
        self._save()


def _write_failure(project: Path | None, request: dict[str, Any], completed: dict[str, Any], exc: Exception) -> dict[str, Any]:
    failure = {
        "schema_version": 1,
        "status": "blocked",
        "qualification_only": True,
        "production_science_released": False,
        "failed_at": _now(),
        "request_sha256": _hash_json(request),
        "completed_stages": completed,
        "error": {"type": type(exc).__name__, "message": str(exc)},
    }
    if project is not None:
        path = _stage_receipt(project, "failure", failure)
        failure["failure_log"] = str(path)
        try:
            register_artifact(str(project), str(path), "g01_vertical_failure_log", "qualification workflow")
        except Exception:
            pass
    return failure


def _parse_lammps_log(log_text: str, expected_atoms: int, nvt_steps: int) -> dict[str, Any]:
    fatal: list[str] = []
    if re.search(r"(?im)^\s*ERROR(?:\s+on\s+proc\s+\d+)?:", log_text):
        fatal.append("ERROR")
    if re.search(r"(?im)^\s*Lost atoms:", log_text):
        fatal.append("Lost atoms")
    if re.search(r"(?i)(?<![A-Za-z0-9_])(?:[-+]?nan|[-+]?inf(?:inity)?)(?![A-Za-z0-9_])", log_text):
        fatal.append("NaN/Inf")
    markers = {name: bool(re.search(rf"MCP_STAGE_{name}_PASS", log_text)) for name in ("RUN0", "MINIMIZATION", "NVT")}
    stage_rows: dict[str, list[dict[str, float]]] = {name: [] for name in ("RUN0", "MINIMIZATION", "NVT")}
    current: str | None = None
    header: list[str] | None = None
    for raw in log_text.splitlines():
        marker = re.search(r"MCP_STAGE_(RUN0|MINIMIZATION|NVT)_BEGIN", raw)
        if marker:
            current = marker.group(1)
            header = None
            continue
        if re.search(r"MCP_STAGE_(RUN0|MINIMIZATION|NVT)_END", raw):
            continue
        fields = raw.split()
        if fields[:2] == ["Step", "Atoms"] and "PotEng" in fields and "Temp" in fields:
            header = fields
            continue
        if current is None or header is None or len(fields) != len(header):
            continue
        try:
            values = [float(item) for item in fields]
        except ValueError:
            continue
        if all(math.isfinite(item) for item in values):
            stage_rows[current].append(dict(zip(header, values)))
    errors = list(fatal)
    if not all(markers.values()):
        errors.append("one or more LAMMPS stage completion markers are missing")
    for stage, rows in stage_rows.items():
        if not rows:
            errors.append(f"{stage} produced no finite thermo row")
        for row in rows:
            if int(row.get("Atoms", -1)) != expected_atoms:
                errors.append(f"{stage} atom count changed")
            if not all(math.isfinite(float(row.get(key, float("nan")))) for key in ("Temp", "PotEng", "TotEng")):
                errors.append(f"{stage} temperature or energy is non-finite")
    if len(stage_rows["NVT"]) < 1:
        errors.append("NVT smoke test produced no thermo sample")
    return {
        "status": "pass" if not errors else "fail",
        "errors": errors,
        "lost_atoms": "Lost atoms" in fatal,
        "non_finite": "NaN/Inf" in fatal,
        "stage_markers": markers,
        "stage_rows": stage_rows,
        "nvt_requested_steps": nvt_steps,
    }


def _run_lammps_smoke(data: Path, output: Path, *, temperature: float, minimization_iterations: int, nvt_steps: int, timestep_fs: float, seed: int, timeout_seconds: int) -> dict[str, Any]:
    if output.exists():
        raise FileExistsError(f"LAMMPS qualification output already exists: {output}")
    config = load_pipeline_config()
    executable = approved_executable(config["software"]["lammps"]["executable"], config=config)
    output.mkdir(parents=True, exist_ok=False)
    shutil.copy2(data, output / "input.data")
    script = output / "in.g01_vertical"
    script.write_text(f"""# G01 qualification-only smoke path; not a production trajectory.
clear
units real
dimension 3
boundary f f f
atom_style full
pair_style lj/class2/coul/cut 12.5
pair_modify mix sixthpower
bond_style class2
angle_style class2
special_bonds lj/coul 0.0 0.0 1.0
read_data input.data
neighbor 2.0 bin
neigh_modify every 1 delay 0 check yes
thermo 1
thermo_style custom step atoms temp pe etotal press vol
thermo_modify flush yes lost error
print MCP_STAGE_RUN0_BEGIN
run 0 post yes
print MCP_STAGE_RUN0_PASS
print MCP_STAGE_RUN0_END
print MCP_STAGE_MINIMIZATION_BEGIN
min_style cg
minimize 1.0e-6 1.0e-8 {minimization_iterations} {minimization_iterations * 5}
print MCP_STAGE_MINIMIZATION_PASS
print MCP_STAGE_MINIMIZATION_END
velocity all create {temperature:.8f} {seed} mom yes rot yes dist gaussian
timestep {timestep_fs:.8f}
dump g01 all custom 1 trajectory.lammpstrj id mol type q x y z xu yu zu ix iy iz
dump_modify g01 sort id first yes
fix g01_nvt all nvt temp {temperature:.8f} {temperature:.8f} 100.0
print MCP_STAGE_NVT_BEGIN
run {nvt_steps} post yes
print MCP_STAGE_NVT_PASS
print MCP_STAGE_NVT_END
unfix g01_nvt
undump g01
write_data final.data
""", encoding="ascii", newline="\n")
    command = [str(executable), "-in", script.name, "-log", "log.lammps", "-screen", "screen.txt"]
    try:
        completed = subprocess.run(command, cwd=output, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", timeout=timeout_seconds, check=False, shell=False, close_fds=True)
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"LAMMPS qualification smoke timed out after {timeout_seconds}s") from exc
    log_path = output / "log.lammps"
    log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""
    parsed = _parse_lammps_log(log_text, int(inspect_lammps_data(str(data))["header_counts"]["atoms"]), nvt_steps)
    data_preflight = inspect_lammps_data(str(output / "final.data"), periodic_axes=()) if (output / "final.data").is_file() else {"status": "missing"}
    if completed.returncode != 0:
        parsed["errors"].append(f"LAMMPS returned {completed.returncode}")
    if data_preflight.get("status") != "pass":
        parsed["errors"].append("final LAMMPS data failed static preflight")
    parsed.update({
        "return_code": completed.returncode,
        "command": command,
        "runtime": {"executable": str(executable), "sha256": _sha256(executable)},
        "parameters": {"temperature_kelvin": temperature, "minimization_iterations": minimization_iterations, "nvt_steps": nvt_steps, "timestep_fs": timestep_fs, "random_seed": seed},
        "data_preflight": data_preflight,
        "artifacts": {name: {"path": str(path), "sha256": _sha256(path)} for name, path in (("input_data", output / "input.data"), ("input_script", script), ("log", log_path), ("trajectory", output / "trajectory.lammpstrj"), ("final_data", output / "final.data")) if path.is_file()},
        "stderr_tail": completed.stderr[-2000:],
    })
    parsed["status"] = "pass" if not parsed["errors"] else "fail"
    _write_json(output / "lammps_qualification_receipt.json", parsed)
    return parsed


def run_g01_qualification_vertical(*, project_id: str, input_xsd: str, input_sha256: str | None = None, projects_root: str | None = None, forcefield_file: str | None = None, forcefield_off: str | None = None, temperature_kelvin: float = 50.0, minimization_iterations: int = 100, nvt_steps: int = 20, timestep_fs: float = 1.0, random_seed: int = 173017, timeout_seconds: int = 300, dry_run: bool = True, confirmation_token: str | None = None) -> dict[str, Any]:
    profile = _profile()
    safe_id = _safe_project_id(project_id)
    config = load_pipeline_config()
    source = resolve_workspace_path(input_xsd, config=config, must_exist=True)
    if source.suffix.lower() != ".xsd":
        raise ValueError("input_xsd must be an XSD file")
    digest = _sha256(source)
    if input_sha256 is not None and _require_hash(input_sha256, "input_sha256") != digest:
        raise ValueError("input_sha256 does not match the input XSD")
    audit = _audit_input(source, profile)
    if audit["status"] != "pass":
        raise ValueError("G01 structure audit failed: " + "; ".join(audit["errors"]))
    frc = Path(forcefield_file or (Path(config["software"]["lammps"]["frc_files"]) / "pcff.frc")).resolve(strict=True)
    off = Path(forcefield_off or (Path(config["software"]["materials_studio"]["root"]) / "share" / "Resources" / "Simulation" / "ClassicalEnergy" / "FORCEFIELDS" / "Standard" / "pcff.off")).resolve(strict=True)
    frc_sha = _sha256(frc)
    off_sha = _sha256(off)
    if frc_sha != profile["pcff_frc_sha256"] or off_sha != profile["pcff_off_sha256"]:
        raise ValueError("G01 reviewed PCFF parameter hashes do not match the local files")
    if isinstance(minimization_iterations, bool) or not isinstance(minimization_iterations, int) or not 1 <= minimization_iterations <= profile["max_minimization_iterations"]:
        raise ValueError(f"minimization_iterations must be 1..{profile['max_minimization_iterations']}")
    if isinstance(nvt_steps, bool) or not isinstance(nvt_steps, int) or not 1 <= nvt_steps <= profile["max_nvt_steps"]:
        raise ValueError(f"nvt_steps must be 1..{profile['max_nvt_steps']}")
    if isinstance(temperature_kelvin, bool) or not isinstance(temperature_kelvin, (int, float)) or not 1 <= float(temperature_kelvin) <= 300:
        raise ValueError("temperature_kelvin must be from 1 to 300 for the bounded G01 smoke test")
    if isinstance(timestep_fs, bool) or not isinstance(timestep_fs, (int, float)) or not 0.1 <= float(timestep_fs) <= 1.0:
        raise ValueError("timestep_fs must be from 0.1 to 1.0")
    if isinstance(random_seed, bool) or not isinstance(random_seed, int) or not 1 <= random_seed <= 2147483647:
        raise ValueError("random_seed must be an integer in the LAMMPS range")
    request = {"project_id": safe_id, "input_xsd": str(source), "input_sha256": digest, "projects_root": projects_root, "forcefield_file": str(frc), "forcefield_off": str(off), "temperature_kelvin": float(temperature_kelvin), "minimization_iterations": minimization_iterations, "nvt_steps": nvt_steps, "timestep_fs": float(timestep_fs), "random_seed": random_seed, "timeout_seconds": timeout_seconds}
    planned_root = (Path(projects_root).resolve() if projects_root else Path(config["policy"]["workspace_roots"][0]).resolve() / "projects") / safe_id
    planned_project = planned_root
    planned_outputs = [str(planned_project / "manifest.json"), str(planned_project / _V2_MANIFEST_NAME), str(planned_project / "reports" / _REPORT_NAME)]
    plan = {
        "status": "dry_run" if dry_run else "execution_requested",
        "qualification_only": True,
        "production_science_released": False,
        "dry_run": dry_run,
        "writes_performed": False,
        "execution_started": False,
        "reviewed_profile": profile,
        "steps": [
            {"index": 1, "name": "initialize_project", "status": "planned"},
            {"index": 2, "name": "register_input_xsd", "status": "planned", "input_sha256": digest},
            {"index": 3, "name": "structure_audit", "status": audit["status"], "audit": audit},
            {"index": 4, "name": "forcite_preparation", "profile_id": profile["preparation_profile_id"], "status": "planned"},
            {"index": 5, "name": "forcefield_and_topology_postflight", "status": "planned"},
            {"index": 6, "name": "forcite_energy", "profile_id": profile["energy_profile_id"], "status": "planned"},
            {"index": 7, "name": "export_car_mdf", "status": "planned"},
            {"index": 8, "name": "convert_lammps_data", "status": "planned", "forcefield_class": profile["forcefield_class"]},
            {"index": 9, "name": "lammps_run_0", "status": "planned"},
            {"index": 10, "name": "lammps_short_minimization", "status": "planned", "iterations": minimization_iterations},
            {"index": 11, "name": "lammps_nvt_smoke", "status": "planned", "steps": nvt_steps, "temperature_kelvin": float(temperature_kelvin), "timestep_fs": float(timestep_fs), "random_seed": random_seed},
            {"index": 12, "name": "vmd_text_validation", "status": "planned"},
            {"index": 13, "name": "qualification_report", "status": "planned"},
        ],
        "planned_outputs": planned_outputs,
        "parameters_sha256": _hash_json(request),
        "confirmation_required_for_real_execution": True,
        "confirmation_parameters": request,
        "next_actions": ["Review the exact parameters and issue one confirmation for md_g01_qualification_vertical before dry_run=false."],
    }
    if dry_run:
        if planned_project.exists():
            raise FileExistsError(f"Project already exists; refusing to overwrite: {planned_project}")
        return plan
    if not confirmation_token:
        raise PermissionError("A confirmation_token is required for real qualification execution")
    confirmation_manager.consume(confirmation_token, "md_g01_qualification_vertical", request)
    project: Path | None = None
    completed: dict[str, Any] = {}
    try:
        created = initialize_project(safe_id, "G01 PCFF MS-LAMMPS-VMD qualification", projects_root=str(planned_root.parent))
        project = Path(created["project_directory"]).resolve()
        state_path = project / _STATE_NAME
        state = {"schema_version": 1, "workflow": "g01_pcff_vertical_v1", "request_sha256": _hash_json(request), "started_at": _now(), "stages": completed}
        _write_json(state_path, state)
        source_copy = project / "request" / "g01_input.xsd"
        if not source_copy.exists():
            shutil.copy2(source, source_copy)
        elif _sha256(source_copy) != digest:
            raise ValueError("Existing project input XSD hash differs from the confirmed request")
        register_artifact(str(project), str(source_copy), "g01_input_xsd", "hash-bound reviewed G01 fixture")
        _advance_v1_status(project, "specified")
        v2 = _V2Ledger(project, safe_id)
        source_id = v2.artifact("source-xsd", "source_structure", source_copy, [])
        structure_receipt = _stage_receipt(project, "structure_audit", audit)
        structure_ev = v2.evidence("evidence-structure", "STRUCTURE_VERIFIED", structure_receipt, [source_id], kind="MODEL_GEOMETRY")
        v2.manifest = set_target_artifact_v2(v2.manifest, source_id)
        v2.state("STRUCTURE_VERIFIED", [structure_ev])
        completed["structure_audit"] = audit
        _write_json(state_path, {**state, "stages": completed})
        forcefield_metadata = {"name": "PCFF water calibrated subset", "version": "MS pcff 3.1 / msi2lmp pcff.frc 4.0", "units": "real", "atom_typing_source": "Materials Studio Forcite forcefield assignment", "charge_model": "forcefield assigned partial charges", "mixing_rule": "sixthpower", "special_bonds": "lj/coul 0.0 0.0 1.0", "parameter_sources": [str(off), str(frc)]}
        science_contract = {"schema_version": 1, "units": {"profile": "ms_pcff_to_lammps_real_v1", "lammps_style": "real", "length": "angstrom", "time": "femtosecond", "energy": "kcal/mol", "charge": "elementary_charge", "mass": "g/mol", "pressure": "atm"}, "coordinates": {"representation": "cartesian", "length_unit": "angstrom", "cell_matrix_convention": "row_vectors_a_b_c"}, "boundary": {"periodic_axes": [], "lammps_boundary": ["f", "f", "f"]}, "lammps": {"atom_style": "full", "triclinic_tilt_handling": "preserve"}, "charge": {"formal_charge_semantics": "oxidation_or_connectivity_only", "partial_charge_semantics": "forcefield_nonbonded_charge", "partial_charge_source": "Materials Studio PCFF 3.1", "expected_net_partial_charge_e": 0.0, "audit_by_component": True}, "forcefield": {"profile_id": "g01-pcff-water-subset-v1", "profile_version": "1.0.0", "profile_sha256": _hash_json(forcefield_metadata), "parameter_file_sha256": [off_sha, frc_sha], "compatibility_reviewed": True, "mixing_rule": "sixthpower", "special_bonds": "lj/coul 0.0 0.0 1.0", "long_range_electrostatics": "none_nonperiodic"}, "seed_ledger": {"master_seed": random_seed, "derivation": "sha256_run_uuid_stage_replica_to_int31_v1", "replica_index": 0, "streams": {"packing": random_seed, "velocity": random_seed, "thermostat": random_seed, "sorption": random_seed}}, "trajectory": {"wrapped_fields": "x_y_z", "unwrapped_fields": "xu_yu_zu", "msd_uses_unwrapped": True, "density_uses_wrapped": True, "timestep_unit": "femtosecond"}}
        update_model_specification(str(project), {"composition": [{"name": "H2O", "count": 1}], "temperature_k": float(temperature_kelvin), "pressure_mpa": None, "periodic_axes": [], "cell": {"kind": "nonperiodic_bounding_box"}, "fixed_regions": []}, forcefield_metadata, science_contract, {"fidelity": "reviewed_calibration_fixture", "clay_is_surrogate": False, "source_model": str(source)})
        preparation_parameters = {"project_directory": str(project), "input_structure": str(source_copy), "input_sha256": digest, "profile_id": profile["preparation_profile_id"], "calculation_parameters": None, "output_slot": "g01_vertical_prepared", "idempotency_key": f"{safe_id}-g01-preparation-v1", "timeout_seconds": timeout_seconds}
        preparation = _checked_call("ms_forcite_calculation_checked", preparation_parameters, ttl_seconds=timeout_seconds)
        prepared = Path(preparation["output_structure"])
        prepared_audit = inspect_structure_preflight(str(prepared))
        prep_postflight = preparation.get("forcefield_preparation_audit") or {}
        if prep_postflight.get("status") != "pass":
            raise RuntimeError(f"Forcite preparation postflight failed: {prep_postflight.get('errors')}")
        prepared_id = v2.artifact("prepared-xsd", "derived_structure", prepared, [source_id])
        bundle_path = project / "forcefield" / "g01_pcff_vertical_bundle.json"
        _write_json(bundle_path, {"profile": profile, "parameter_files": {"pcff_off": {"path": str(off), "sha256": off_sha}, "pcff_frc": {"path": str(frc), "sha256": frc_sha}}, "charge_audit": prep_postflight, "qualification_only": True})
        ff_id = v2.artifact("forcefield-bundle", "forcefield_bundle", bundle_path, [prepared_id], metadata={"forcefield_profile": profile["preparation_profile_id"], "profile_sha256": _hash_json(forcefield_metadata), "parameter_file_sha256": [off_sha, frc_sha], "atom_typing_coverage": 1, "charge_audit": {"status": "VERIFIED", "partial_charge_coverage": 1, "net_charge_e": float(prep_postflight.get("net_partial_charge_e") or 0.0), "expected_net_charge_e": 0.0, "tolerance_e": float(prep_postflight.get("net_charge_tolerance_e") or 1e-4)}})
        prep_receipt = _stage_receipt(project, "forcite_preparation", preparation)
        ff_ev = v2.evidence("evidence-forcefield", "FORCEFIELD_VERIFIED", prep_receipt, [prepared_id, ff_id], kind="MODEL_GEOMETRY")
        v2.state("FORCEFIELD_VERIFIED", [ff_ev])
        _advance_v1_status(project, "modelled")
        completed["forcite_preparation"] = {"output": str(prepared), "postflight": prep_postflight}
        _write_json(state_path, {**state, "stages": completed})
        energy_parameters = {"project_directory": str(project), "input_structure": str(prepared), "input_sha256": _sha256(prepared), "profile_id": profile["energy_profile_id"], "calculation_parameters": None, "output_slot": "g01_vertical_energy", "idempotency_key": f"{safe_id}-g01-energy-v1", "timeout_seconds": timeout_seconds}
        energy = _checked_call("ms_forcite_calculation_checked", energy_parameters, ttl_seconds=timeout_seconds)
        energy_structure = Path(energy["output_structure"])
        energy_audit = inspect_structure_preflight(str(energy_structure))
        topology_unchanged = all(energy_audit.get(key) == audit["audit"].get(key) for key in ("atom_count", "elements", "bond_count", "bond_types"))
        if energy_audit.get("status") != "pass" or not topology_unchanged:
            raise RuntimeError("Forcite Energy output failed structure or topology preservation postflight")
        energy_id = v2.artifact("energy-xsd", "derived_structure", energy_structure, [prepared_id])
        energy_receipt = _stage_receipt(project, "forcite_energy", {"result": energy, "audit": energy_audit, "topology_unchanged": topology_unchanged})
        completed["forcite_energy"] = {"output": str(energy_structure), "audit": energy_audit, "topology_unchanged": topology_unchanged}
        _write_json(state_path, {**state, "stages": completed})
        export_parameters = {"project_directory": str(project), "input_xsd": str(energy_structure), "input_sha256": _sha256(energy_structure), "output_slot": "g01_vertical_car_mdf", "idempotency_key": f"{safe_id}-g01-export-v1", "timeout_seconds": timeout_seconds}
        exported = _checked_call("md_export_xsd_to_car_mdf_checked", export_parameters, ttl_seconds=timeout_seconds)
        car, mdf = Path(exported["output_car"]), Path(exported["output_mdf"])
        car_id = v2.artifact("car", "conversion_artifact", car, [energy_id])
        mdf_id = v2.artifact("mdf", "conversion_artifact", mdf, [energy_id])
        export_receipt = _stage_receipt(project, "export_car_mdf", exported)
        completed["export_car_mdf"] = {"car": str(car), "mdf": str(mdf), "car_sha256": _sha256(car), "mdf_sha256": _sha256(mdf)}
        _write_json(state_path, {**state, "stages": completed})
        conversion_parameters = {"project_directory": str(project), "car_path": str(car), "car_sha256": _sha256(car), "mdf_path": str(mdf), "mdf_sha256": _sha256(mdf), "forcefield_file": str(frc), "forcefield_sha256": frc_sha, "forcefield_class": profile["forcefield_class"], "output_slot": "g01_vertical_lammps", "idempotency_key": f"{safe_id}-g01-conversion-v1", "timeout_seconds": timeout_seconds}
        converted = _checked_call("md_convert_to_lammps_checked", conversion_parameters, ttl_seconds=timeout_seconds)
        data = Path(converted["output_data"])
        data_id = v2.artifact("lammps-data", "conversion_artifact", data, [car_id, mdf_id, ff_id])
        conversion_receipt = _stage_receipt(project, "conversion", converted)
        conversion_ev = v2.evidence("evidence-conversion", "CONVERSION_VERIFIED", conversion_receipt, [data_id], kind="INTERFACE_CONVERSION")
        v2.state("CONVERSION_VERIFIED", [conversion_ev])
        _advance_v1_status(project, "converted")
        completed["conversion"] = {"data": str(data), "data_sha256": _sha256(data)}
        _write_json(state_path, {**state, "stages": completed})
        smoke_dir = project / "lammps" / "logs" / "g01_vertical_smoke_001"
        smoke = _run_lammps_smoke(data, smoke_dir, temperature=float(temperature_kelvin), minimization_iterations=minimization_iterations, nvt_steps=nvt_steps, timestep_fs=float(timestep_fs), seed=random_seed, timeout_seconds=timeout_seconds)
        if smoke.get("status") != "pass":
            raise RuntimeError(f"LAMMPS qualification smoke failed: {smoke.get('errors')}")
        lammps_receipt_path = smoke_dir / "lammps_qualification_receipt.json"
        sim_id = v2.artifact("simulation-run", "simulation_run", lammps_receipt_path, [data_id])
        preflight_receipt = _stage_receipt(project, "lammps_preflight", smoke)
        preflight_ev = v2.evidence("evidence-lammps", "LAMMPS_PREFLIGHT_VERIFIED", preflight_receipt, [sim_id], kind="SOFTWARE_FUNCTION")
        v2.state("LAMMPS_PREFLIGHT_VERIFIED", [preflight_ev])
        vmd_dir = project / "vmd" / "g01_vertical_smoke_001"
        vmd = validate_vmd_text_trajectory(str(data), str(smoke_dir / "trajectory.lammpstrj"), str(vmd_dir), expected_frames=nvt_steps + 1, timeout_seconds=60)
        if vmd.get("status") != "pass":
            raise RuntimeError(f"VMD qualification validation failed: {vmd.get('errors')}")
        vmd_evidence_path = Path(vmd_dir) / "vmd_validation_evidence.json"
        analysis_id = v2.artifact("vmd-analysis", "analysis_result", vmd_evidence_path, [sim_id])
        qualification_receipt = _stage_receipt(project, "qualification", {"lammps": smoke, "vmd": vmd, "qualification_only": True, "production_science_released": False})
        qualification_ev = v2.evidence("evidence-qualification", "QUALIFICATION_ONLY", qualification_receipt, [analysis_id], kind="SOFTWARE_FUNCTION", status="QUALIFICATION_ONLY")
        v2.state("QUALIFICATION_ONLY", [qualification_ev])
        _advance_v1_status(project, "preflight_passed")
        for gate, evidence in (("structure", audit), ("forcefield", prep_postflight), ("lammps_preflight", smoke)):
            _record_verified_quality_gate(str(project), gate, validator=f"g01_vertical_qualification:{gate}:v1", passed=True, evidence={"status": "pass", "qualification_only": True, "evidence": evidence})
        set_quality_gate(str(project), "scientific_validation", "blocked", {"reason": "The bounded smoke test is qualification evidence only; it is not production science."})
        report = {"schema_version": 1, "workflow": "g01_pcff_vertical_v1", "status": "qualification_pass", "qualification_only": True, "production_science_released": False, "created_at": _now(), "project_directory": str(project), "input": {"path": str(source_copy), "sha256": digest, "structure_audit": audit}, "outputs": {"prepared_xsd": {"path": str(prepared), "sha256": _sha256(prepared)}, "energy_xsd": {"path": str(energy_structure), "sha256": _sha256(energy_structure)}, "car": {"path": str(car), "sha256": _sha256(car)}, "mdf": {"path": str(mdf), "sha256": _sha256(mdf)}, "lammps_data": {"path": str(data), "sha256": _sha256(data)}, "lammps_final_data": smoke.get("artifacts", {}).get("final_data"), "trajectory": smoke.get("artifacts", {}).get("trajectory"), "vmd_validation_evidence": {"path": str(vmd_evidence_path), "sha256": _sha256(vmd_evidence_path)}}, "tools": {"mcp": release_identity(), "materials_studio": "Materials Studio 23.1 / governed MaterialsScript", "lammps": smoke.get("runtime"), "vmd": vmd.get("runtime")}, "parameters": request, "random_seed": random_seed, "gates": {"structure": "pass", "forcefield": "pass", "conversion": "pass", "lammps_run0": "pass", "lammps_minimization": "pass", "lammps_nvt_smoke": "pass", "vmd": "pass", "scientific_validation": "blocked_qualification_only"}, "failure_logs": [], "v2_manifest": str(v2.path), "limitations": ["This is a bounded G01 calibration qualification only.", "The short NVT smoke test is not a paper production trajectory.", "No production science release is implied."]}
        report_path = project / "reports" / _REPORT_NAME
        _write_json(report_path, report)
        register_artifact(str(project), str(report_path), "g01_vertical_qualification_report", "qualification workflow")
        completed["report"] = {"path": str(report_path), "sha256": _sha256(report_path)}
        _write_json(state_path, {**state, "finished_at": _now(), "status": "qualification_pass", "stages": completed})
        return {"status": "qualification_pass", "qualification_only": True, "production_science_released": False, "project_directory": str(project), "report_path": str(report_path), "report_sha256": _sha256(report_path), "v2_manifest_path": str(v2.path), "artifact_ids": [item["artifact_id"] for item in v2.manifest["artifacts"] if item["artifact_type"] != "evidence_receipt"], "evidence_ids": [item["artifact_id"] for item in v2.manifest["artifacts"] if item["artifact_type"] == "evidence_receipt"], "blockers": [], "gates": report["gates"], "next_actions": ["Review the qualification report; do not cite this smoke test as a production trajectory."]}
    except Exception as exc:
        failure = _write_failure(project, request, completed, exc)
        return {"status": "blocked", "qualification_only": True, "production_science_released": False, "project_directory": str(project) if project else None, "artifact_ids": [], "evidence_ids": [], "blockers": [failure["error"]["message"]], "failure": failure, "next_actions": ["Inspect the preserved failure log, correct the cause, and rerun under a new non-overwriting project_id."]}


__all__ = ["run_g01_qualification_vertical"]
