from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .pipeline_config import load_pipeline_config, resolve_workspace_path
from .structure_preflight import inspect_lammps_data


DEFAULT_VMD_TIMEOUT_SECONDS = 60
_SAFE_TCL = """# Generated safety-constant VMD text-mode validation script.
proc mcp_report {tag trajectory} {
    set molid [mol new $trajectory type lammpstrj waitfor all]
    set atoms [molinfo $molid get numatoms]
    set frames [molinfo $molid get numframes]
    set cell [molinfo $molid get {a b c alpha beta gamma}]
    set selection [atomselect $molid all frame 0]
    set finite_count [llength [$selection get {x y z}]]
    $selection delete
    puts "MCP_VMD_${tag}_ATOMS $atoms"
    puts "MCP_VMD_${tag}_FRAMES $frames"
    puts "MCP_VMD_${tag}_CELL $cell"
    puts "MCP_VMD_${tag}_FINITE_COORD_ROWS $finite_count"
    mol delete $molid
}
mcp_report WRAPPED trajectory.wrapped.lammpstrj
mcp_report UNWRAPPED trajectory.unwrapped_for_vmd.lammpstrj
puts "MCP_VMD_VALIDATION_COMPLETE 1"
quit
"""

_SAFE_G05_TCL = """# Generated safety-constant G05 triclinic VMD text validation.
proc mcp_g05_report {tag trajectory} {
    set molid [mol new $trajectory type lammpstrj waitfor all]
    set atoms [molinfo $molid get numatoms]
    set frames [molinfo $molid get numframes]
    molinfo $molid set frame 0
    set cell [molinfo $molid get {a b c alpha beta gamma}]
    set selection [atomselect $molid all frame 0]
    set sx 0.0
    set sy 0.0
    set sz 0.0
    foreach xyz [$selection get {x y z}] {
        set sx [expr {$sx + [lindex $xyz 0]}]
        set sy [expr {$sy + [lindex $xyz 1]}]
        set sz [expr {$sz + [lindex $xyz 2]}]
    }
    puts "MCP_G05_${tag}_ATOMS $atoms"
    puts "MCP_G05_${tag}_FRAMES $frames"
    puts "MCP_G05_${tag}_CELL $cell"
    puts "MCP_G05_${tag}_COORDSUM $sx $sy $sz"
    $selection delete
    mol delete $molid
}
mcp_g05_report WRAPPED trajectory.wrapped.lammpstrj
mcp_g05_report UNWRAPPED trajectory.unwrapped_for_vmd.lammpstrj
puts "MCP_G05_VALIDATION_COMPLETE 1"
quit
"""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _project_dump(source: Path, destination: Path, *, unwrapped: bool) -> dict[str, Any]:
    """Project an audited LAMMPS dump to fixed columns understood by VMD."""
    lines = source.read_text(encoding="utf-8", errors="strict").splitlines()
    output: list[str] = []
    index = 0
    frames = 0
    atom_counts: list[int] = []
    boxes: list[list[list[float]]] = []
    boundary_flags: list[list[str]] = []
    required_coordinates = ("xu", "yu", "zu") if unwrapped else ("x", "y", "z")
    while index < len(lines):
        if lines[index].strip() != "ITEM: TIMESTEP" or index + 1 >= len(lines):
            raise ValueError(f"Malformed dump at line {index + 1}: expected ITEM: TIMESTEP")
        output.extend(["ITEM: TIMESTEP", lines[index + 1].strip()])
        index += 2
        if index + 1 >= len(lines) or lines[index].strip() != "ITEM: NUMBER OF ATOMS":
            raise ValueError("Malformed dump: missing atom-count header")
        atom_count = int(lines[index + 1].strip())
        if atom_count <= 0:
            raise ValueError("Dump frame has no atoms")
        atom_counts.append(atom_count)
        output.extend(["ITEM: NUMBER OF ATOMS", str(atom_count)])
        index += 2
        if index >= len(lines) or not lines[index].startswith("ITEM: BOX BOUNDS"):
            raise ValueError("Malformed dump: missing box bounds")
        box_header = lines[index].strip()
        header_fields = box_header.split()[3:]
        if any(name in header_fields for name in ("xy", "xz", "yz")):
            raise ValueError("Triclinic dump projection requires the dedicated G05 validator")
        flags = header_fields[-3:]
        if len(flags) != 3 or any(flag not in {"pp", "ff", "ss", "mm", "fs", "sf", "fm", "mf"} for flag in flags):
            raise ValueError("Dump boundary flags are missing or invalid")
        boundary_flags.append(flags)
        output.append(box_header)
        index += 1
        frame_box: list[list[float]] = []
        for _ in range(3):
            values = [float(item) for item in lines[index].split()[:2]]
            if len(values) != 2 or not all(math.isfinite(value) for value in values) or values[1] <= values[0]:
                raise ValueError("Dump contains invalid box bounds")
            frame_box.append(values)
            output.append(f"{values[0]:.16g} {values[1]:.16g}")
            index += 1
        boxes.append(frame_box)
        if index >= len(lines) or not lines[index].startswith("ITEM: ATOMS "):
            raise ValueError("Malformed dump: missing atom columns")
        columns = lines[index].split()[2:]
        required = ("id", "mol", "type", "q", *required_coordinates)
        missing = [name for name in required if name not in columns]
        if missing:
            raise ValueError(f"Dump is missing required columns: {missing}")
        positions = [columns.index(name) for name in required]
        output.append("ITEM: ATOMS id mol type q x y z")
        index += 1
        seen_ids: set[int] = set()
        for _ in range(atom_count):
            fields = lines[index].split()
            selected = [fields[position] for position in positions]
            atom_id = int(selected[0])
            numeric = [float(value) for value in selected[3:]]
            if atom_id in seen_ids or not all(math.isfinite(value) for value in numeric):
                raise ValueError("Dump contains duplicate atom IDs or non-finite coordinates/charge")
            seen_ids.add(atom_id)
            output.append(" ".join(selected))
            index += 1
        frames += 1
    if len(set(atom_counts)) != 1:
        raise ValueError("Atom count changes across dump frames")
    if any(flags != boundary_flags[0] for flags in boundary_flags[1:]):
        raise ValueError("Boundary flags change across dump frames")
    destination.write_text("\n".join(output) + "\n", encoding="utf-8", newline="\n")
    return {"frames": frames, "atoms_per_frame": atom_counts[0],
            "first_box_bounds_angstrom": boxes[0], "boundary_flags": boundary_flags[0]}


def _parse_vmd_markers(text: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for tag in ("WRAPPED", "UNWRAPPED"):
        for field in ("ATOMS", "FRAMES", "FINITE_COORD_ROWS"):
            match = re.search(rf"^MCP_VMD_{tag}_{field}\s+(\d+)\s*$", text, re.MULTILINE)
            result[f"{tag.lower()}_{field.lower()}"] = int(match.group(1)) if match else None
        cell = re.search(rf"^MCP_VMD_{tag}_CELL\s+(.+?)\s*$", text, re.MULTILINE)
        if cell:
            try:
                values = [float(item) for item in cell.group(1).split()]
                result[f"{tag.lower()}_cell"] = values if len(values) == 6 and all(math.isfinite(v) for v in values) else None
            except ValueError:
                result[f"{tag.lower()}_cell"] = None
        else:
            result[f"{tag.lower()}_cell"] = None
    result["complete"] = bool(re.search(r"^MCP_VMD_VALIDATION_COMPLETE\s+1\s*$", text, re.MULTILINE))
    return result


def _cell_lengths_angles(cell_rows: list[list[float]]) -> list[float]:
    lengths = [math.sqrt(sum(value * value for value in row)) for row in cell_rows]
    def angle(first: list[float], second: list[float], denominator: float) -> float:
        cosine = sum(a * b for a, b in zip(first, second)) / denominator
        return math.degrees(math.acos(max(-1.0, min(1.0, cosine))))
    a, b, c = cell_rows
    return [*lengths, angle(b, c, lengths[1] * lengths[2]),
            angle(a, c, lengths[0] * lengths[2]), angle(a, b, lengths[0] * lengths[1])]


def _project_g05_triclinic_dump(source: Path, destination: Path, *, unwrapped: bool) -> dict[str, Any]:
    """Create a fixed-column VMD projection while preserving triclinic BOX records."""
    lines = source.read_text(encoding="utf-8", errors="strict").splitlines()
    output: list[str] = []
    index = frames = 0
    atom_counts: list[int] = []
    reference_cell: list[list[float]] | None = None
    reference_tilt: list[float] | None = None
    reference_flags: list[str] | None = None
    first_coordinate_sum = [0.0, 0.0, 0.0]
    coordinates = ("xu", "yu", "zu") if unwrapped else ("x", "y", "z")
    while index < len(lines):
        if lines[index].strip() != "ITEM: TIMESTEP" or index + 1 >= len(lines):
            raise ValueError(f"Malformed triclinic dump at line {index + 1}")
        output.extend(["ITEM: TIMESTEP", lines[index + 1].strip()]); index += 2
        if index + 1 >= len(lines) or lines[index].strip() != "ITEM: NUMBER OF ATOMS":
            raise ValueError("Triclinic dump is missing atom count")
        atom_count = int(lines[index + 1].strip())
        if atom_count <= 0:
            raise ValueError("Triclinic dump frame has no atoms")
        atom_counts.append(atom_count)
        output.extend(["ITEM: NUMBER OF ATOMS", str(atom_count)]); index += 2
        if index >= len(lines):
            raise ValueError("Triclinic dump is missing BOX BOUNDS")
        header = lines[index].split()
        if header[:3] != ["ITEM:", "BOX", "BOUNDS"] or header[3:6] != ["xy", "xz", "yz"]:
            raise ValueError("G05 requires BOX BOUNDS xy xz yz")
        flags = header[6:9]
        if len(flags) != 3 or any(flag not in {"pp", "ff", "ss", "mm", "fs", "sf", "fm", "mf"} for flag in flags):
            raise ValueError("Triclinic dump boundary flags are invalid")
        output.append(" ".join(header)); index += 1
        rows: list[list[float]] = []
        for _ in range(3):
            values = [float(item) for item in lines[index].split()]
            if len(values) != 3 or not all(math.isfinite(value) for value in values):
                raise ValueError("Triclinic BOX BOUNDS rows must each contain three finite values")
            rows.append(values)
            output.append(" ".join(f"{value:.16g}" for value in values)); index += 1
        xlo_bound, xhi_bound, xy = rows[0]
        ylo_bound, yhi_bound, xz = rows[1]
        zlo, zhi, yz = rows[2]
        xlo = xlo_bound - min(0.0, xy, xz, xy + xz)
        xhi = xhi_bound - max(0.0, xy, xz, xy + xz)
        ylo = ylo_bound - min(0.0, yz)
        yhi = yhi_bound - max(0.0, yz)
        cell = [[xhi - xlo, 0.0, 0.0], [xy, yhi - ylo, 0.0], [xz, yz, zhi - zlo]]
        if any(cell[i][i] <= 0 for i in range(3)):
            raise ValueError("Triclinic dump has a non-positive true box length")
        if reference_cell is None:
            reference_cell, reference_tilt, reference_flags = cell, [xy, xz, yz], flags
        elif cell != reference_cell or [xy, xz, yz] != reference_tilt or flags != reference_flags:
            raise ValueError("G05 cell, tilt, or boundary flags change across frames")
        if index >= len(lines) or not lines[index].startswith("ITEM: ATOMS "):
            raise ValueError("Triclinic dump is missing atom columns")
        columns = lines[index].split()[2:]
        required = ("id", "mol", "type", "q", *coordinates)
        missing = [name for name in required if name not in columns]
        if missing:
            raise ValueError(f"Triclinic dump is missing required columns: {missing}")
        if not all(name in columns for name in ("ix", "iy", "iz", "x", "y", "z", "xu", "yu", "zu")):
            raise ValueError("G05 source must explicitly carry wrapped, unwrapped, and image-flag columns")
        positions = [columns.index(name) for name in required]
        output.append("ITEM: ATOMS id mol type q x y z"); index += 1
        seen_ids: set[int] = set()
        for _ in range(atom_count):
            fields = lines[index].split()
            selected = [fields[position] for position in positions]
            atom_id = int(selected[0])
            numeric = [float(value) for value in selected[3:]]
            if atom_id in seen_ids or not all(math.isfinite(value) for value in numeric):
                raise ValueError("G05 contains duplicate IDs or non-finite values")
            seen_ids.add(atom_id)
            if frames == 0:
                for axis in range(3):
                    first_coordinate_sum[axis] += float(selected[4 + axis])
            output.append(" ".join(selected)); index += 1
        frames += 1
    if len(set(atom_counts)) != 1 or reference_cell is None or reference_tilt is None or reference_flags is None:
        raise ValueError("G05 atom count changes or dump has no frames")
    destination.write_text("\n".join(output) + "\n", encoding="utf-8", newline="\n")
    return {"frames": frames, "atoms_per_frame": atom_counts[0], "cell_rows_angstrom": reference_cell,
            "cell_lengths_angles": _cell_lengths_angles(reference_cell), "tilt_angstrom": reference_tilt,
            "boundary_flags": reference_flags, "first_coordinate_sum_angstrom": first_coordinate_sum}


def _parse_g05_markers(text: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for tag in ("WRAPPED", "UNWRAPPED"):
        for field in ("ATOMS", "FRAMES"):
            match = re.search(rf"^MCP_G05_{tag}_{field}\s+(\d+)\s*$", text, re.MULTILINE)
            result[f"{tag.lower()}_{field.lower()}"] = int(match.group(1)) if match else None
        for field, length in (("CELL", 6), ("COORDSUM", 3)):
            match = re.search(rf"^MCP_G05_{tag}_{field}\s+(.+?)\s*$", text, re.MULTILINE)
            try:
                values = [float(item) for item in match.group(1).split()] if match else []
            except ValueError:
                values = []
            result[f"{tag.lower()}_{field.lower()}"] = values if len(values) == length and all(math.isfinite(v) for v in values) else None
    result["complete"] = bool(re.search(r"^MCP_G05_VALIDATION_COMPLETE\s+1\s*$", text, re.MULTILINE))
    return result


def validate_vmd_text_trajectory(data_path: str, dump_path: str, output_directory: str,
                                 *, expected_frames: int | None = None,
                                 timeout_seconds: int = DEFAULT_VMD_TIMEOUT_SECONDS) -> dict[str, Any]:
    """Validate wrapped and unwrapped trajectory views using VMD without a GUI."""
    if not 1 <= timeout_seconds <= DEFAULT_VMD_TIMEOUT_SECONDS:
        raise ValueError(f"timeout_seconds must be between 1 and {DEFAULT_VMD_TIMEOUT_SECONDS}")
    loaded = load_pipeline_config()
    data = resolve_workspace_path(data_path, config=loaded, must_exist=True)
    dump = resolve_workspace_path(dump_path, config=loaded, must_exist=True)
    destination = resolve_workspace_path(output_directory, config=loaded)
    if destination.exists():
        raise FileExistsError(f"VMD validation output directory already exists: {destination}")
    data_preflight = inspect_lammps_data(str(data))
    if data_preflight["status"] != "pass":
        return {"schema_version": 1, "status": "blocked_static_preflight", "executed": False,
                "data_preflight": data_preflight}

    destination.mkdir(parents=True, exist_ok=False)
    shutil.copy2(data, destination / "candidate.data")
    wrapped_meta = _project_dump(dump, destination / "trajectory.wrapped.lammpstrj", unwrapped=False)
    unwrapped_meta = _project_dump(dump, destination / "trajectory.unwrapped_for_vmd.lammpstrj", unwrapped=True)
    projection_errors: list[str] = []
    expected_atoms = data_preflight["header_counts"].get("atoms")
    for name, metadata in (("wrapped", wrapped_meta), ("unwrapped", unwrapped_meta)):
        if metadata["atoms_per_frame"] != expected_atoms:
            projection_errors.append(f"{name} dump atom count differs from LAMMPS data")
        if expected_frames is not None and metadata["frames"] != expected_frames:
            projection_errors.append(f"{name} dump has {metadata['frames']} frames, expected {expected_frames}")
    if wrapped_meta["boundary_flags"] != unwrapped_meta["boundary_flags"]:
        projection_errors.append("wrapped and unwrapped boundary flags differ")
    if projection_errors:
        result = {"schema_version": 1, "status": "blocked_dump_preflight", "executed": False,
                  "data_preflight": data_preflight, "wrapped": wrapped_meta,
                  "unwrapped": unwrapped_meta, "errors": projection_errors}
        (destination / "vmd_validation_evidence.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return result

    script = destination / "validate_trajectory.tcl"
    script.write_text(_SAFE_TCL, encoding="utf-8", newline="\n")
    executable = Path(loaded["software"]["vmd"]["executable"])
    environment = dict(os.environ)
    environment["VMDNOCUDA"] = "1"
    command = [str(executable), "-dispdev", "text", "-e", script.name]
    started = _utc_now()
    timed_out = False
    return_code: int | None = None
    combined = ""
    try:
        completed = subprocess.run(command, cwd=destination, stdin=subprocess.DEVNULL,
                                   capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=timeout_seconds,
                                   check=False, shell=False, env=environment, close_fds=True)
        return_code = completed.returncode
        combined = completed.stdout + "\n" + completed.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        combined = stdout + "\n" + stderr
    (destination / "vmd_text_output.log").write_text(combined, encoding="utf-8", errors="replace")
    parsed = _parse_vmd_markers(combined)
    errors: list[str] = []
    if timed_out:
        errors.append("VMD text validation timed out")
    if return_code != 0:
        errors.append(f"VMD returned exit code {return_code}")
    if not parsed["complete"]:
        errors.append("VMD completion marker is missing")
    for tag, metadata in (("wrapped", wrapped_meta), ("unwrapped", unwrapped_meta)):
        if parsed[f"{tag}_atoms"] != expected_atoms:
            errors.append(f"VMD {tag} atom count mismatch")
        if parsed[f"{tag}_frames"] != metadata["frames"]:
            errors.append(f"VMD {tag} frame count mismatch")
        if parsed[f"{tag}_finite_coord_rows"] != expected_atoms:
            errors.append(f"VMD {tag} coordinate row count mismatch")
        cell = parsed[f"{tag}_cell"]
        expected_lengths = [bounds[1] - bounds[0] for bounds in metadata["first_box_bounds_angstrom"]]
        if cell is None or any(abs(cell[i] - expected_lengths[i]) > 1.0e-6 for i in range(3)):
            errors.append(f"VMD {tag} cell lengths differ from dump box")

    flags = wrapped_meta["boundary_flags"]
    periodic_axes = [axis for axis, flag in zip(("x", "y", "z"), flags) if flag.startswith("p")]
    result = {
        "schema_version": 1,
        "validator": "materials_studio_mcp.vmd_validation",
        "status": "pass" if not errors else "fail",
        "executed": True,
        "started_utc": started,
        "finished_utc": _utc_now(),
        "semantics": {
            "length_unit": "angstrom",
            "source_dump_coordinates": ["wrapped:x,y,z", "unwrapped:xu,yu,zu"],
            "vmd_projection": "unwrapped coordinates are relabeled x,y,z in a separate audited copy",
            "periodic_axes": periodic_axes,
            "lammps_dump_boundary_flags": flags,
        },
        "runtime": {"executable_name": executable.name, "executable_sha256": _sha256(executable),
                    "return_code": return_code, "timed_out": timed_out, "display_mode": "text"},
        "data_preflight": data_preflight,
        "wrapped_projection": wrapped_meta,
        "unwrapped_projection": unwrapped_meta,
        "vmd_observed": parsed,
        "artifacts": {
            "source_data_sha256": _sha256(data), "source_dump_sha256": _sha256(dump),
            "wrapped_dump_sha256": _sha256(destination / "trajectory.wrapped.lammpstrj"),
            "unwrapped_dump_sha256": _sha256(destination / "trajectory.unwrapped_for_vmd.lammpstrj"),
            "tcl_sha256": _sha256(script),
            "vmd_log_sha256": _sha256(destination / "vmd_text_output.log"),
        },
        "errors": errors,
    }
    (destination / "vmd_validation_evidence.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def validate_g05_triclinic_vmd(data_path: str, dump_path: str, output_directory: str,
                               *, expected_frames: int | None = None,
                               timeout_seconds: int = DEFAULT_VMD_TIMEOUT_SECONDS) -> dict[str, Any]:
    """Dedicated fail-closed VMD gate for triclinic G05 trajectories."""
    if not 1 <= timeout_seconds <= DEFAULT_VMD_TIMEOUT_SECONDS:
        raise ValueError(f"timeout_seconds must be between 1 and {DEFAULT_VMD_TIMEOUT_SECONDS}")
    loaded = load_pipeline_config()
    data = resolve_workspace_path(data_path, config=loaded, must_exist=True)
    dump = resolve_workspace_path(dump_path, config=loaded, must_exist=True)
    destination = resolve_workspace_path(output_directory, config=loaded)
    if destination.exists():
        raise FileExistsError(f"VMD validation output directory already exists: {destination}")
    preflight = inspect_lammps_data(str(data), periodic_axes=("x", "y", "z"))
    cell = preflight.get("cell") or {}
    errors: list[str] = []
    if preflight["status"] != "pass":
        errors.append("LAMMPS data preflight failed")
    if cell.get("kind") != "restricted_triclinic":
        errors.append("G05 data is not restricted triclinic")
    if errors:
        return {"schema_version": 1, "status": "blocked_static_preflight", "executed": False,
                "data_preflight": preflight, "errors": errors}

    destination.mkdir(parents=True, exist_ok=False)
    shutil.copy2(data, destination / "candidate.data")
    try:
        wrapped = _project_g05_triclinic_dump(dump, destination / "trajectory.wrapped.lammpstrj", unwrapped=False)
        unwrapped = _project_g05_triclinic_dump(dump, destination / "trajectory.unwrapped_for_vmd.lammpstrj", unwrapped=True)
    except Exception as exc:
        result = {"schema_version": 1, "status": "blocked_dump_preflight", "executed": False,
                  "data_preflight": preflight, "errors": [str(exc)]}
        (destination / "vmd_validation_evidence.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return result
    expected_atoms = preflight["header_counts"].get("atoms")
    for name, metadata in (("wrapped", wrapped), ("unwrapped", unwrapped)):
        if metadata["atoms_per_frame"] != expected_atoms:
            errors.append(f"{name} atom count differs from data")
        if expected_frames is not None and metadata["frames"] != expected_frames:
            errors.append(f"{name} frame count differs from expected {expected_frames}")
        if metadata["cell_rows_angstrom"] != cell["cell_rows"]:
            errors.append(f"{name} triclinic cell differs from data")
        if metadata["tilt_angstrom"] != [cell["tilt"][key] for key in ("xy", "xz", "yz")]:
            errors.append(f"{name} tilt differs from data")
        if metadata["boundary_flags"] != ["pp", "pp", "pp"]:
            errors.append(f"{name} boundary is not pp pp pp")
    if wrapped["first_coordinate_sum_angstrom"] == unwrapped["first_coordinate_sum_angstrom"]:
        errors.append("Wrapped and unwrapped G05 projections are unexpectedly identical")
    if errors:
        result = {"schema_version": 1, "status": "blocked_dump_preflight", "executed": False,
                  "data_preflight": preflight, "wrapped_projection": wrapped,
                  "unwrapped_projection": unwrapped, "errors": errors}
        (destination / "vmd_validation_evidence.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return result

    script = destination / "validate_g05.tcl"
    script.write_text(_SAFE_G05_TCL, encoding="utf-8", newline="\n")
    executable = Path(loaded["software"]["vmd"]["executable"])
    environment = dict(os.environ)
    environment["VMDNOCUDA"] = "1"
    started = _utc_now()
    timed_out = False
    return_code: int | None = None
    combined = ""
    try:
        completed = subprocess.run(
            [str(executable), "-dispdev", "text", "-e", script.name], cwd=destination,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout_seconds, check=False, shell=False, env=environment,
        )
        return_code = completed.returncode
        combined = completed.stdout + "\n" + completed.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        combined = (exc.stdout if isinstance(exc.stdout, str) else "") + "\n" + (exc.stderr if isinstance(exc.stderr, str) else "")
    (destination / "vmd_text_output.log").write_text(combined, encoding="utf-8", errors="replace")
    observed = _parse_g05_markers(combined)
    runtime_errors: list[str] = []
    if timed_out:
        runtime_errors.append("VMD text validation timed out")
    if return_code != 0:
        runtime_errors.append(f"VMD returned exit code {return_code}")
    if not observed["complete"]:
        runtime_errors.append("VMD completion marker is missing")
    for name, metadata in (("wrapped", wrapped), ("unwrapped", unwrapped)):
        if observed[f"{name}_atoms"] != expected_atoms:
            runtime_errors.append(f"VMD {name} atom count mismatch")
        if observed[f"{name}_frames"] != metadata["frames"]:
            runtime_errors.append(f"VMD {name} frame count mismatch")
        expected_cell = metadata["cell_lengths_angles"]
        actual_cell = observed[f"{name}_cell"]
        if actual_cell is None or any(abs(a - b) > 1.0e-5 for a, b in zip(actual_cell, expected_cell)):
            runtime_errors.append(f"VMD {name} triclinic lengths/angles mismatch")
        expected_sum = metadata["first_coordinate_sum_angstrom"]
        actual_sum = observed[f"{name}_coordsum"]
        if actual_sum is None or any(abs(a - b) > 1.0e-4 for a, b in zip(actual_sum, expected_sum)):
            runtime_errors.append(f"VMD {name} coordinate semantics mismatch")
    result = {
        "schema_version": 1, "validator": "materials_studio_mcp.vmd_validation.g05",
        "status": "pass" if not runtime_errors else "fail", "executed": True,
        "started_utc": started, "finished_utc": _utc_now(),
        "semantics": {"length_unit": "angstrom", "periodic_axes": ["x", "y", "z"],
                      "source_columns": ["x/y/z", "xu/yu/zu", "ix/iy/iz"],
                      "triclinic_tilt_preserved": True},
        "runtime": {"executable_name": executable.name, "executable_sha256": _sha256(executable),
                    "return_code": return_code, "timed_out": timed_out, "display_mode": "text",
                    "vmdnocuda": True},
        "data_preflight": preflight, "wrapped_projection": wrapped,
        "unwrapped_projection": unwrapped, "vmd_observed": observed,
        "artifacts": {"source_data_sha256": _sha256(data), "source_dump_sha256": _sha256(dump),
                      "wrapped_dump_sha256": _sha256(destination / "trajectory.wrapped.lammpstrj"),
                      "unwrapped_dump_sha256": _sha256(destination / "trajectory.unwrapped_for_vmd.lammpstrj"),
                      "tcl_sha256": _sha256(script),
                      "vmd_log_sha256": _sha256(destination / "vmd_text_output.log")},
        "errors": runtime_errors,
    }
    (destination / "vmd_validation_evidence.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result
