from __future__ import annotations

import math
import re
import itertools
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from typing import Any

from .pipeline_config import load_pipeline_config, resolve_workspace_path
from .triclinic import image_unwrap, minimum_image_displacement, parse_restricted_triclinic_box
from .triclinic import AXES, cartesian_to_fractional


def _vector(value: str | None) -> list[float] | None:
    try:
        values = [float(item) for item in (value or "").split(",")]
        return values if len(values) == 3 and all(math.isfinite(item) for item in values) else None
    except ValueError:
        return None


def _determinant(a: list[float], b: list[float], c: list[float]) -> float:
    return (a[0] * (b[1] * c[2] - b[2] * c[1])
            - a[1] * (b[0] * c[2] - b[2] * c[0])
            + a[2] * (b[0] * c[1] - b[1] * c[0]))


def _minimum_distance(coordinates: list[list[float]], vectors: dict[str, list[float] | None] | None) -> float | None:
    if len(coordinates) < 2:
        return None
    best = math.inf
    periodic = bool(vectors and all(vectors.values()))
    for index, first in enumerate(coordinates[:-1]):
        for second in coordinates[index + 1:]:
            delta = [first[i] - second[i] for i in range(3)]
            if periodic:
                delta = [item - round(item) for item in delta]
                av, bv, cv = vectors["AVector"], vectors["BVector"], vectors["CVector"]
                cart = [delta[0] * av[i] + delta[1] * bv[i] + delta[2] * cv[i] for i in range(3)]
            else:
                cart = delta
            best = min(best, math.sqrt(sum(item * item for item in cart)))
    return best


def _binned_lammps_minimum_distance(
    coordinates: list[list[float]], box: dict[str, Any], periodic_axes: tuple[str, ...],
    initial_cutoff: float,
) -> float | None:
    """Find the exact periodic minimum with fractional bins, including skewed boxes."""

    if len(coordinates) < 2:
        return None
    origin = box["true_origin"]
    periodic = set(periodic_axes)
    fractional = [cartesian_to_fractional(item, box) for item in coordinates]
    for values in fractional:
        for axis in range(3):
            if AXES[axis] in periodic:
                values[axis] -= math.floor(values[axis])

    # A Cartesian displacement shorter than r changes fractional component i by
    # at most r*||row_i(H^-1)||.  Bin widths at least that large guarantee that
    # a pair within r lies in the same or an adjacent fractional bin.
    dual_norms: list[float] = []
    base = cartesian_to_fractional(origin, box)
    for component in range(3):
        coefficients = []
        for cart_axis in range(3):
            point = list(origin)
            point[cart_axis] += 1.0
            transformed = cartesian_to_fractional(point, box)
            coefficients.append(transformed[component] - base[component])
        dual_norms.append(math.sqrt(sum(value * value for value in coefficients)))

    cutoff = max(float(initial_cutoff), 1.0e-6)
    maximum_span = max(box["true_lengths"]) * 2.0
    while cutoff <= maximum_span:
        bin_counts = [max(1, int(1.0 / (cutoff * norm))) for norm in dual_norms]
        buckets: dict[tuple[int, int, int], list[int]] = {}
        keys: list[tuple[int, int, int]] = []
        for atom_index, values in enumerate(fractional):
            key_values: list[int] = []
            for axis in range(3):
                if AXES[axis] in periodic:
                    key_values.append(min(bin_counts[axis] - 1, int(values[axis] * bin_counts[axis])))
                else:
                    # Nonperiodic coordinates are uncommon for LAMMPS data preflight;
                    # use the same cell width without wrapping.
                    key_values.append(math.floor(values[axis] * bin_counts[axis]))
            key = tuple(key_values)
            keys.append(key)
            buckets.setdefault(key, []).append(atom_index)

        best = math.inf
        for left, key in enumerate(keys):
            neighbor_keys: set[tuple[int, int, int]] = set()
            for delta in itertools.product((-1, 0, 1), repeat=3):
                neighbor: list[int] = []
                for axis in range(3):
                    value = key[axis] + delta[axis]
                    if AXES[axis] in periodic:
                        value %= bin_counts[axis]
                    neighbor.append(value)
                neighbor_keys.add(tuple(neighbor))
            for neighbor in neighbor_keys:
                for right in buckets.get(neighbor, []):
                    if right <= left:
                        continue
                    displacement = minimum_image_displacement(
                        coordinates[left], coordinates[right], box, periodic_axes
                    )
                    best = min(best, math.sqrt(sum(value * value for value in displacement)))
        if math.isfinite(best) and best <= cutoff * (1.0 + 1.0e-12):
            return best
        cutoff = min(best, cutoff * 2.0) if math.isfinite(best) else cutoff * 2.0
    raise RuntimeError("Could not resolve the LAMMPS minimum atom distance with bounded spatial bins")


def inspect_xsd_preflight(path: str, charge_tolerance: float = 1e-6,
                          minimum_distance_angstrom: float = 0.35,
                          allow_net_charge: bool = False) -> dict[str, Any]:
    source = resolve_workspace_path(path, must_exist=True)
    if not source.is_file() or source.suffix.lower() != ".xsd":
        raise FileNotFoundError(f"XSD file not found: {source}")
    root = ET.parse(source).getroot()
    # Materials Studio serializes periodic display images as lightweight
    # Atom3d/Bond3d nodes with ImageOf. They are references, not independent
    # unit-cell topology records, and have no XYZ or forcefield attributes.
    atoms = [item for item in root.iter("Atom3d") if item.get("ImageOf") is None]
    bond_nodes = [item for item in root.iter("Bond") if item.get("ImageOf") is None]
    bond3d_nodes = [item for item in root.iter("Bond3d") if item.get("ImageOf") is None]
    # Current MS 23.1 XSD exports use Bond, while some older fixtures use
    # Bond3d. Prefer Bond if both are present to avoid double-counting.
    bonds = bond_nodes or bond3d_nodes
    space_groups = list(root.iter("SpaceGroup"))
    elements, ff_types = Counter(), Counter()
    bond_types = Counter((item.get("Type") or item.get("BondType") or "Single").strip() for item in bonds)
    missing_xyz = missing_ff = 0
    formal_charge = 0.0
    formal_charge_known = True
    atom_ids: set[str] = set()
    connection_ids: set[str] = set()
    duplicate_ids: list[str] = []
    coordinates: list[list[float]] = []
    for atom in atoms:
        atom_id = atom.get("ID", "")
        if atom_id in atom_ids:
            duplicate_ids.append(atom_id)
        atom_ids.add(atom_id)
        element = (atom.get("Components") or atom.get("Name") or "unknown").split(",")[0]
        elements[element] += 1
        ff_type = (atom.get("ForcefieldType") or "").strip()
        if ff_type:
            ff_types[ff_type] += 1
        else:
            missing_ff += 1
        xyz = _vector(atom.get("XYZ"))
        if xyz is None:
            missing_xyz += 1
        else:
            coordinates.append(xyz)
        connection_ids.update(item.strip() for item in (atom.get("Connections") or "").split(",") if item.strip())
        raw_charge = atom.get("FormalCharge")
        if raw_charge:
            try:
                numerator, denominator = (raw_charge.split("/", 1) + ["1"])[:2]
                formal_charge += float(numerator) / float(denominator)
            except (ValueError, ZeroDivisionError):
                formal_charge_known = False
    cell = None
    if space_groups:
        sg = space_groups[0]
        vectors = {key: _vector(sg.get(key)) for key in ("AVector", "BVector", "CVector")}
        volume = abs(_determinant(vectors["AVector"], vectors["BVector"], vectors["CVector"])) if all(vectors.values()) else None
        cell = {"vectors": vectors, "valid": bool(all(vectors.values()) and volume and volume > 1e-8),
                "volume": volume, "space_group": sg.get("GroupName")}
    errors, warnings = [], []
    if not atoms: errors.append("No Atom3d records found")
    if missing_xyz: errors.append(f"{missing_xyz} atoms have invalid or missing XYZ coordinates")
    if duplicate_ids: errors.append(f"Duplicate atom IDs found: {duplicate_ids[:10]}")
    if missing_ff: errors.append(f"{missing_ff} atoms do not have ForcefieldType; forcefield coverage is incomplete")
    if not cell: warnings.append("No periodic SpaceGroup/cell found")
    elif not cell["valid"]: errors.append("Periodic cell vectors are invalid")
    if formal_charge_known and not allow_net_charge and abs(formal_charge) > charge_tolerance:
        errors.append(f"Unexpected formal net charge {formal_charge:.8g} exceeds tolerance {charge_tolerance}")
    if not formal_charge_known: errors.append("Formal charge contains a non-finite or invalid value")
    minimum_distance = _minimum_distance(coordinates, cell["vectors"] if cell and cell["valid"] else None)
    if minimum_distance is not None and minimum_distance < minimum_distance_angstrom:
        errors.append(f"Minimum atom distance {minimum_distance:.6g} A is below {minimum_distance_angstrom} A")
    return {"validator": "materials_studio_mcp.structure_preflight", "schema_version": 1,
            "format": "xsd", "path": str(source), "status": "pass" if not errors else "fail",
            "atom_count": len(atoms), "bond_count": len(bonds) or len(connection_ids),
            "explicit_bond_node_count": len(bonds), "connection_reference_count": len(connection_ids), "elements": dict(elements),
            "bond_types": dict(bond_types),
            "forcefield_types": dict(ff_types), "missing_forcefield_type_count": missing_ff,
            "formal_charge": formal_charge if formal_charge_known else None, "cell": cell,
            "minimum_atom_distance_angstrom": minimum_distance,
            "minimum_distance_threshold_angstrom": minimum_distance_angstrom,
            "errors": errors, "warnings": warnings}


SECTION_NAMES = {"Masses", "Atoms", "Bonds", "Angles", "Dihedrals", "Impropers", "Velocities",
                 "Pair Coeffs", "Bond Coeffs", "Angle Coeffs", "Dihedral Coeffs", "Improper Coeffs"}


def inspect_lammps_data(path: str, charge_tolerance: float = 1e-6,
                        allow_net_charge: bool = False,
                        minimum_distance_angstrom: float = 0.35,
                        periodic_axes: tuple[str, ...] = ("x", "y", "z")) -> dict[str, Any]:
    source = resolve_workspace_path(path, must_exist=True)
    if not source.is_file():
        raise FileNotFoundError(f"LAMMPS data file not found: {source}")
    lines = source.read_text(encoding="utf-8", errors="replace").splitlines()
    header_counts: dict[str, int] = {}
    for line in lines[:100]:
        match = re.match(r"^\s*(\d+)\s+(atoms|bonds|angles|dihedrals|impropers|atom types|bond types|angle types|dihedral types|improper types)\s*$", line)
        if match: header_counts[match.group(2)] = int(match.group(1))
    sections: dict[str, list[str]] = {}
    current = None
    for raw in lines:
        clean = raw.split("#", 1)[0].strip()
        matched = next((name for name in SECTION_NAMES if clean == name or clean.startswith(name + " ")), None)
        if matched:
            current = matched
            sections.setdefault(current, [])
            continue
        if current and clean:
            if any(clean == name or clean.startswith(name + " ") for name in SECTION_NAMES):
                continue
            sections[current].append(clean)
    errors, warnings = [], []
    if header_counts.get("atoms", 0) <= 0: errors.append("LAMMPS model is empty or has no positive atom count")
    invalid_axes = sorted(set(periodic_axes) - {"x", "y", "z"})
    if invalid_axes: errors.append(f"Invalid periodic axes: {invalid_axes}")
    try:
        box = parse_restricted_triclinic_box(lines)
    except ValueError as exc:
        box = None
        errors.append(f"Invalid simulation box value: {exc}")
    if box and not box.get("valid"):
        errors.extend(box.get("errors", []))
    box_bounds = box.get("bounds", {}) if box else {}
    mapping = {"atoms": "Atoms", "bonds": "Bonds", "angles": "Angles", "dihedrals": "Dihedrals", "impropers": "Impropers"}
    for count_name, section_name in mapping.items():
        expected = header_counts.get(count_name, 0)
        actual = len(sections.get(section_name, []))
        if expected != actual: errors.append(f"Header says {expected} {count_name}, but {section_name} contains {actual} records")
    atom_types = header_counts.get("atom types", 0)
    mass_count = len(sections.get("Masses", []))
    if atom_types and mass_count != atom_types: errors.append(f"Header says {atom_types} atom types, but Masses contains {mass_count} records")
    mass_types: set[int] = set()
    for row in sections.get("Masses", []):
        fields = row.split()
        try:
            type_id, mass = int(fields[0]), float(fields[1])
            if type_id < 1 or type_id > atom_types: errors.append(f"Mass type ID {type_id} is outside 1..{atom_types}")
            if not math.isfinite(mass) or mass <= 0: errors.append(f"Mass for type {type_id} must be finite and positive")
            if type_id in mass_types: errors.append(f"Duplicate Masses type ID: {type_id}")
            mass_types.add(type_id)
        except (ValueError, IndexError): errors.append(f"Invalid Masses row: {row}")
    atoms_heading = next((raw.strip() for raw in lines if raw.strip().startswith("Atoms")), None)
    if atoms_heading != "Atoms # full":
        errors.append("LAMMPS data must declare exactly 'Atoms # full'")
    charges, atom_ids, used_types, coordinates, image_flags, unwrapped = [], set(), set(), [], {}, []
    for row in sections.get("Atoms", []):
        fields = row.split()
        if len(fields) in {7, 10}:
            try:
                atom_id, type_id, charge = int(fields[0]), int(fields[2]), float(fields[3])
                values = [charge, *map(float, fields[4:7])]
                if atom_id in atom_ids: errors.append(f"Duplicate atom ID: {atom_id}")
                atom_ids.add(atom_id); used_types.add(type_id)
                if not 1 <= type_id <= atom_types: errors.append(f"Atom type ID {type_id} is outside 1..{atom_types}")
                if not all(math.isfinite(item) for item in values): errors.append(f"Atom {atom_id} contains NaN or infinite values")
                else:
                    charges.append(charge)
                    coordinates.append(values[1:])
                    image = [0, 0, 0] if len(fields) == 7 else [int(item) for item in fields[7:10]]
                    image_flags[atom_id] = image
                    if box and box.get("valid"):
                        unwrapped.append(image_unwrap(values[1:], image, box))
            except ValueError: errors.append(f"Invalid Atoms row: {row}")
        else:
            errors.append(f"Atoms # full row must have 7 fields or 10 with image flags: {row}")
    total_charge = sum(charges) if charges else None
    if total_charge is None: warnings.append("Could not infer charges; confirm the Atoms style explicitly")
    elif abs(total_charge) > charge_tolerance and not allow_net_charge:
        errors.append(f"Unexpected net charge {total_charge:.8g} exceeds tolerance {charge_tolerance}")
    missing_mass_types = sorted(used_types - mass_types)
    if missing_mass_types: errors.append(f"Atom types without valid Masses entries: {missing_mass_types}")
    minimum_distance = None
    if len(coordinates) >= 2:
        if box and box.get("valid"):
            minimum_distance = _binned_lammps_minimum_distance(
                coordinates, box, periodic_axes, minimum_distance_angstrom
            )
        else:
            minimum_distance = _minimum_distance(coordinates, None)
    if minimum_distance is not None and minimum_distance < minimum_distance_angstrom:
        errors.append(f"Minimum atom distance {minimum_distance:.6g} A is below {minimum_distance_angstrom} A")
    return {"validator": "materials_studio_mcp.structure_preflight", "schema_version": 1,
            "format": "lammps-data", "path": str(source), "status": "pass" if not errors else "fail",
            "header_counts": header_counts, "section_counts": {key: len(value) for key, value in sections.items()},
            "inferred_total_charge": total_charge, "charge_tolerance": charge_tolerance,
            "used_atom_types": sorted(used_types), "mass_types": sorted(mass_types),
            "box_bounds": box_bounds, "cell": box, "periodic_axes": list(periodic_axes),
            "image_flags_present": any(any(value != 0 for value in image) for image in image_flags.values()),
            "image_flags_by_atom": {str(key): value for key, value in sorted(image_flags.items())},
            "unwrapped_coordinates_angstrom": unwrapped,
            "coordinate_semantics": {"wrapped": "x/y/z in Atoms # full", "unwrapped": "wrapped + ix*a + iy*b + iz*c"},
            "minimum_atom_distance_angstrom": minimum_distance,
            "errors": errors, "warnings": warnings}


def inspect_structure_preflight(path: str, charge_tolerance: float = 1e-6) -> dict[str, Any]:
    suffix = Path(path).suffix.lower()
    if suffix == ".xsd": return inspect_xsd_preflight(path)
    if suffix in {".data", ".lmp", ".lammps"} or Path(path).name.lower().startswith("data."):
        return inspect_lammps_data(path, charge_tolerance)
    raise ValueError("Supported formats are XSD and LAMMPS data files")


def inspect_msi2lmp_inputs(car_path: str, mdf_path: str | None = None, forcefield_file: str | None = None,
                           forcefield_class: str = "I") -> dict[str, Any]:
    loaded = load_pipeline_config()
    car = resolve_workspace_path(car_path, config=loaded)
    mdf = resolve_workspace_path(mdf_path, config=loaded) if mdf_path else car.with_suffix(".mdf")
    config = loaded["software"]["lammps"]
    library = Path(config["frc_files"])
    allowed_classes = {"I", "II", "O", "1", "2", "0"}
    errors, warnings = [], []
    if not car.is_file(): errors.append(f"CAR file not found: {car}")
    if not mdf.is_file(): errors.append(f"MDF file not found: {mdf}")
    if car.stem.casefold() != mdf.stem.casefold(): errors.append("CAR and MDF basenames must match")
    car_text = car.read_text(encoding="utf-8", errors="replace") if car.is_file() else ""
    mdf_text = mdf.read_text(encoding="utf-8", errors="replace") if mdf.is_file() else ""
    if car.is_file() and (not car_text.strip() or "!BIOSYM archive" not in car_text):
        errors.append("CAR file is empty or lacks a BIOSYM archive header")
    if mdf.is_file() and (not mdf_text.strip() or "!BIOSYM molecular_data" not in mdf_text):
        errors.append("MDF file is empty or lacks a BIOSYM molecular_data header")
    car_atom_lines = [line for line in car_text.splitlines() if line.strip() and not line.lstrip().startswith(("!", "PBC=", "PBC ", "end", "Materials Studio"))]
    mdf_atom_lines = [line for line in mdf_text.splitlines() if line.lstrip().startswith("@molecule") or ":" in line]
    if car.is_file() and not car_atom_lines: errors.append("CAR model contains no detectable atom records")
    if mdf.is_file() and not mdf_atom_lines: errors.append("MDF model contains no detectable molecular/atom records")
    if forcefield_class.upper() not in allowed_classes: errors.append("forcefield_class must be I, II, O, 1, 2, or 0")
    frc = None
    if forcefield_file:
        candidate = Path(forcefield_file)
        if candidate.is_absolute() or candidate.parent != Path("."):
            frc = candidate.expanduser().resolve(strict=False)
        else:
            name = candidate.name if candidate.suffix else candidate.name + ".frc"
            frc = library / name
        library_resolved = library.resolve(strict=False)
        try:
            frc.relative_to(library_resolved)
        except ValueError:
            errors.append(f"Forcefield parameter file must be inside configured frc_files library: {frc}")
        if not frc.is_file(): errors.append(f"Forcefield parameter file not found: {frc}")
    else:
        warnings.append("No msi2lmp forcefield file selected; conversion must not proceed on an implicit default")
    return {"validator": "materials_studio_mcp.msi2lmp_input_preflight", "schema_version": 1,
            "status": "pass" if not errors and frc else ("blocked" if errors else "needs_forcefield_selection"),
            "car_path": str(car), "mdf_path": str(mdf), "forcefield_class": forcefield_class,
            "forcefield_file": str(frc) if frc else None, "available_forcefields": sorted(item.name for item in library.glob("*.frc")),
            "car_detected_record_count": len(car_atom_lines), "mdf_detected_record_count": len(mdf_atom_lines),
            "errors": errors, "warnings": warnings}
