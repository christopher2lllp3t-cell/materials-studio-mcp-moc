from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .pipeline_config import load_pipeline_config, resolve_workspace_path
from .structure_preflight import inspect_lammps_data


FIXED_VALIDATION_SEED = 4928459
MAX_VALIDATION_STEPS = 500
DEFAULT_MD_STEPS = 100
DEFAULT_TIMESTEP_FS = 0.25
DEFAULT_TEMPERATURE_K = 50.0
DEFAULT_TIMEOUT_SECONDS = 60

_FATAL_PATTERNS = {
    "lammps_error": re.compile(r"(^|\n)ERROR(?: on proc \d+)?:", re.IGNORECASE),
    "lost_atoms": re.compile(r"(^|\n)\s*(?:ERROR(?: on proc \d+)?:\s*)?Lost atoms:", re.IGNORECASE),
    "non_numeric": re.compile(r"non[- ]numeric|\b(?:nan|inf)\b", re.IGNORECASE),
    "bond_atoms_missing": re.compile(r"bond atoms missing", re.IGNORECASE),
    "angle_atoms_missing": re.compile(r"angle atoms missing", re.IGNORECASE),
}

_SECTION_NAMES = (
    "Masses", "Pair Coeffs", "Bond Coeffs", "Angle Coeffs", "Dihedral Coeffs", "Improper Coeffs",
    "Atoms", "Bonds", "Angles", "Dihedrals", "Impropers", "Velocities",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parameter_and_topology_gate(path: Path) -> dict[str, Any]:
    """Independently require complete type coefficients and valid topology references."""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    sections: dict[str, list[list[str]]] = {}
    current: str | None = None
    for raw in lines:
        clean = raw.split("#", 1)[0].strip()
        if re.fullmatch(r"[A-Za-z][A-Za-z ]*", clean):
            current = clean if clean in _SECTION_NAMES else None
            if current:
                sections.setdefault(current, [])
            continue
        if current and clean:
            sections[current].append(clean.split())

    counts: dict[str, int] = {}
    for raw in lines[:100]:
        match = re.match(
            r"^\s*(\d+)\s+(atom|bond|angle|dihedral|improper) types\s*$", raw,
            re.IGNORECASE,
        )
        if match:
            counts[match.group(2).lower()] = int(match.group(1))
    errors: list[str] = []
    coverage: dict[str, dict[str, Any]] = {}
    section_by_kind = {
        "atom": "Pair Coeffs", "bond": "Bond Coeffs", "angle": "Angle Coeffs",
        "dihedral": "Dihedral Coeffs", "improper": "Improper Coeffs",
    }
    for kind, count in counts.items():
        section_name = section_by_kind[kind]
        found: set[int] = set()
        for row in sections.get(section_name, []):
            try:
                type_id = int(row[0])
                values = [float(value) for value in row[1:]]
                if not values or not all(math.isfinite(value) for value in values):
                    raise ValueError
                if type_id in found:
                    errors.append(f"Duplicate {section_name} type ID {type_id}")
                found.add(type_id)
            except (ValueError, IndexError):
                errors.append(f"Invalid {section_name} row: {' '.join(row)}")
        expected = set(range(1, count + 1))
        missing = sorted(expected - found)
        extra = sorted(found - expected)
        if missing:
            errors.append(f"Missing {section_name} types: {missing}")
        if extra:
            errors.append(f"Out-of-range {section_name} types: {extra}")
        coverage[kind] = {"expected": count, "found": len(found), "complete": found == expected}

    atom_ids: set[int] = set()
    for row in sections.get("Atoms", []):
        try:
            atom_ids.add(int(row[0]))
        except (ValueError, IndexError):
            pass
    topology_layout = {
        "Bonds": ("bond", 2), "Angles": ("angle", 3),
        "Dihedrals": ("dihedral", 4), "Impropers": ("improper", 4),
    }
    for section_name, (kind, atom_arity) in topology_layout.items():
        record_ids: set[int] = set()
        for row in sections.get(section_name, []):
            try:
                record_id, type_id = int(row[0]), int(row[1])
                references = {int(value) for value in row[2:2 + atom_arity]}
                if record_id in record_ids:
                    errors.append(f"Duplicate {section_name} record ID {record_id}")
                record_ids.add(record_id)
                if not 1 <= type_id <= counts.get(kind, 0):
                    errors.append(f"{section_name} record {record_id} has invalid type {type_id}")
                unknown = sorted(references - atom_ids)
                if unknown:
                    errors.append(f"{section_name} record {record_id} references unknown atoms {unknown}")
                if len(references) != atom_arity:
                    errors.append(f"{section_name} record {record_id} repeats an atom reference")
            except (ValueError, IndexError):
                errors.append(f"Invalid {section_name} row: {' '.join(row)}")
    return {"status": "pass" if not errors else "fail", "coverage": coverage, "errors": errors}


def _phase_rows(log_text: str, begin: str, end: str) -> list[dict[str, float]]:
    start = log_text.find(begin)
    stop = log_text.find(end, start + len(begin)) if start >= 0 else -1
    if start < 0 or stop < 0:
        return []
    rows: list[dict[str, float]] = []
    header: list[str] | None = None
    for line in log_text[start:stop].splitlines():
        fields = line.split()
        if fields and fields[0] == "Step":
            header = fields
            continue
        if not header or len(fields) != len(header):
            continue
        try:
            values = [float(item) for item in fields]
        except ValueError:
            continue
        if all(math.isfinite(item) for item in values):
            rows.append(dict(zip(header, values)))
    return rows


def _write_controlled_input(path: Path, *, md_steps: int, timestep_fs: float,
                            temperature_k: float, seed: int,
                            periodic_axes: tuple[str, ...]) -> None:
    boundary = " ".join("p" if axis in periodic_axes else "f" for axis in ("x", "y", "z"))
    content = f"""# Generated by materials_studio_mcp.lammps_validation
clear
units real
dimension 3
boundary {boundary}
atom_style full
pair_style lj/cut/coul/cut 8.0
bond_style harmonic
angle_style harmonic
read_data candidate.data

special_bonds lj/coul 0.0 0.0 0.5
neighbor 2.0 bin
neigh_modify every 1 delay 0 check yes

thermo 1
thermo_modify flush yes lost error
thermo_style custom step atoms temp pe ke etotal ebond eangle evdwl ecoul press

print \"MCP_PHASE_RUN0_BEGIN\"
run 0 post yes
print \"MCP_PHASE_RUN0_END\"

print \"MCP_PHASE_MIN_BEGIN\"
min_style cg
minimize 1.0e-10 1.0e-12 100 1000
print \"MCP_PHASE_MIN_END\"

reset_timestep 0
timestep {timestep_fs:.12g}
velocity all create {temperature_k:.12g} {seed} mom yes rot yes dist gaussian
fix mcp_integrator all nve
dump mcp_validation_dump all custom 10 trajectory.unwrapped.lammpstrj id mol type q x y z xu yu zu ix iy iz
dump_modify mcp_validation_dump sort id
thermo 10
print \"MCP_PHASE_MD_BEGIN\"
run {md_steps} post yes
print \"MCP_PHASE_MD_END\"
write_data validated_final.data nocoeff
"""
    path.write_text(content, encoding="utf-8", newline="\n")


def validate_lammps_short_chain(
    data_path: str,
    output_directory: str,
    *,
    expected_counts: dict[str, int] | None = None,
    md_steps: int = DEFAULT_MD_STEPS,
    timestep_fs: float = DEFAULT_TIMESTEP_FS,
    temperature_k: float = DEFAULT_TEMPERATURE_K,
    seed: int = FIXED_VALIDATION_SEED,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    max_relative_energy_drift: float = 1.0e-3,
    periodic_axes: tuple[str, ...] = ("x", "y", "z"),
) -> dict[str, Any]:
    """Run a fail-closed run-0/minimization/short-NVE validation in a fresh directory.

    This is an internal validation harness, not a production dynamics runner.  The
    input script is generated here; callers cannot supply arbitrary LAMMPS text.
    """
    if not 1 <= md_steps <= MAX_VALIDATION_STEPS:
        raise ValueError(f"md_steps must be between 1 and {MAX_VALIDATION_STEPS}")
    if not 0.01 <= timestep_fs <= 1.0:
        raise ValueError("timestep_fs must be between 0.01 and 1.0 fs")
    if not 1.0 <= temperature_k <= 500.0:
        raise ValueError("temperature_k must be between 1 and 500 K")
    if seed != FIXED_VALIDATION_SEED:
        raise ValueError(f"validation seed is fixed at {FIXED_VALIDATION_SEED}")
    if not 1 <= timeout_seconds <= DEFAULT_TIMEOUT_SECONDS:
        raise ValueError(f"timeout_seconds must be between 1 and {DEFAULT_TIMEOUT_SECONDS}")
    if not 0 < max_relative_energy_drift <= 0.05:
        raise ValueError("max_relative_energy_drift must be in (0, 0.05]")
    if not set(periodic_axes) <= {"x", "y", "z"} or len(set(periodic_axes)) != len(periodic_axes):
        raise ValueError("periodic_axes must be a unique subset of x, y, z")

    loaded = load_pipeline_config()
    source = resolve_workspace_path(data_path, config=loaded, must_exist=True)
    destination = resolve_workspace_path(output_directory, config=loaded)
    if destination.exists():
        raise FileExistsError(f"Validation output directory already exists: {destination}")

    static = inspect_lammps_data(str(source), periodic_axes=periodic_axes)
    parameter_gate = _parameter_and_topology_gate(source)
    count_errors: list[str] = []
    for name, expected in (expected_counts or {}).items():
        actual = static.get("header_counts", {}).get(name)
        if actual != expected:
            count_errors.append(f"Expected {expected} {name}, found {actual}")
    if static["status"] != "pass" or parameter_gate["status"] != "pass" or count_errors:
        return {
            "schema_version": 1,
            "validator": "materials_studio_mcp.lammps_validation",
            "status": "blocked_static_preflight",
            "executed": False,
            "static_preflight": static,
            "parameter_and_topology_gate": parameter_gate,
            "count_errors": count_errors,
        }

    executable = Path(loaded["software"]["lammps"]["executable"])
    destination.mkdir(parents=True, exist_ok=False)
    candidate = destination / "candidate.data"
    shutil.copy2(source, candidate)
    input_file = destination / "in.validation"
    _write_controlled_input(input_file, md_steps=md_steps, timestep_fs=timestep_fs,
                            temperature_k=temperature_k, seed=seed, periodic_axes=periodic_axes)
    started = _utc_now()
    command = [str(executable), "-in", input_file.name, "-log", "log.lammps", "-screen", "none"]
    timed_out = False
    return_code: int | None = None
    stderr = ""
    try:
        completed = subprocess.run(
            command, cwd=destination, stdin=subprocess.DEVNULL, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout_seconds,
            check=False, shell=False, close_fds=True,
        )
        return_code = completed.returncode
        stderr = completed.stderr[-4000:]
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stderr = (exc.stderr or "")[-4000:] if isinstance(exc.stderr, str) else ""

    log_path = destination / "log.lammps"
    log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""
    fatal_matches = [name for name, pattern in _FATAL_PATTERNS.items() if pattern.search(log_text + "\n" + stderr)]
    phases = {
        "run0": _phase_rows(log_text, "MCP_PHASE_RUN0_BEGIN", "MCP_PHASE_RUN0_END"),
        "minimization": _phase_rows(log_text, "MCP_PHASE_MIN_BEGIN", "MCP_PHASE_MIN_END"),
        "short_md": _phase_rows(log_text, "MCP_PHASE_MD_BEGIN", "MCP_PHASE_MD_END"),
    }
    phase_errors = [f"{name} produced no finite thermo rows" for name, rows in phases.items() if not rows]
    min_initial = phases["minimization"][0].get("PotEng") if phases["minimization"] else None
    min_final = phases["minimization"][-1].get("PotEng") if phases["minimization"] else None
    if min_initial is not None and min_final is not None and min_final > min_initial + 1.0e-8:
        phase_errors.append("Minimization increased potential energy")
    md_initial = phases["short_md"][0].get("TotEng") if phases["short_md"] else None
    md_final = phases["short_md"][-1].get("TotEng") if phases["short_md"] else None
    relative_drift = None
    if md_initial is not None and md_final is not None:
        relative_drift = abs(md_final - md_initial) / max(abs(md_initial), abs(md_final), 1.0)
        if relative_drift > max_relative_energy_drift:
            phase_errors.append(
                f"Short-NVE relative total-energy drift {relative_drift:.6g} exceeds {max_relative_energy_drift:.6g}"
            )
    else:
        phase_errors.append("Short-NVE total-energy endpoints are unavailable")
    final_data = destination / "validated_final.data"
    trajectory = destination / "trajectory.unwrapped.lammpstrj"
    final_preflight: dict[str, Any] | None = None
    if not final_data.is_file():
        phase_errors.append("Final validated data artifact was not produced")
    else:
        final_preflight = inspect_lammps_data(str(final_data), periodic_axes=periodic_axes)
        if final_preflight["status"] != "pass":
            phase_errors.append("Final validated data failed structural preflight")
        for name in ("atoms", "bonds", "angles", "dihedrals", "impropers"):
            before = static["header_counts"].get(name, 0)
            after = final_preflight["header_counts"].get(name, 0)
            if before != after:
                phase_errors.append(f"Final topology count changed for {name}: {before} -> {after}")
    if not trajectory.is_file() or trajectory.stat().st_size == 0:
        phase_errors.append("Unwrapped validation trajectory was not produced")

    passed = return_code == 0 and not timed_out and not fatal_matches and not phase_errors
    result = {
        "schema_version": 1,
        "validator": "materials_studio_mcp.lammps_validation",
        "status": "pass" if passed else "fail",
        "executed": True,
        "started_utc": started,
        "finished_utc": _utc_now(),
        "runtime": {
            "executable_name": executable.name,
            "executable_sha256": _sha256(executable),
            "return_code": return_code,
            "timed_out": timed_out,
            "timeout_seconds": timeout_seconds,
        },
        "protocol": {
            "phases": ["run0", "minimization", "short_nve"],
            "seed": seed,
            "md_steps": md_steps,
            "timestep_fs": timestep_fs,
            "temperature_k": temperature_k,
            "periodic_axes": list(periodic_axes),
            "boundary": ["p" if axis in periodic_axes else "f" for axis in ("x", "y", "z")],
            "max_relative_energy_drift": max_relative_energy_drift,
        },
        "artifacts": {
            "input_data_sha256": _sha256(candidate),
            "controlled_input_sha256": _sha256(input_file),
            "log_sha256": _sha256(log_path) if log_path.is_file() else None,
            "final_data_sha256": _sha256(final_data) if final_data.is_file() else None,
            "unwrapped_trajectory_sha256": _sha256(trajectory) if trajectory.is_file() else None,
        },
        "static_preflight": static,
        "parameter_and_topology_gate": parameter_gate,
        "final_structure_preflight": final_preflight,
        "thermo": {
            "row_counts": {name: len(rows) for name, rows in phases.items()},
            "run0": phases["run0"][-1] if phases["run0"] else None,
            "minimization_initial_pe": min_initial,
            "minimization_final_pe": min_final,
            "short_md_initial_etotal": md_initial,
            "short_md_final_etotal": md_final,
            "short_md_relative_energy_drift": relative_drift,
        },
        "fatal_matches": fatal_matches,
        "errors": phase_errors,
    }
    (destination / "validation_evidence.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return result
