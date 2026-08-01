from __future__ import annotations

from collections import Counter
from fractions import Fraction
import hashlib
import itertools
import math
from pathlib import Path
import re
from typing import Any
import xml.etree.ElementTree as ET


_OUTPUT_SLOT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_SHA256 = re.compile(r"^[0-9A-Fa-f]{64}$")
_ATOM_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
_ELEMENT = re.compile(r"^[A-Z][a-z]?$")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def validate_input_hash(path: Path, expected_sha256: str) -> str:
    if not isinstance(expected_sha256, str) or _SHA256.fullmatch(expected_sha256) is None:
        raise ValueError("input_sha256 must contain exactly 64 hexadecimal characters")
    actual = sha256_file(path)
    if actual != expected_sha256.upper():
        raise ValueError(f"Input SHA-256 mismatch: {actual} != {expected_sha256.upper()}")
    return actual


def validate_output_slot(value: str) -> str:
    if not isinstance(value, str) or _OUTPUT_SLOT.fullmatch(value) is None:
        raise ValueError("output_slot must match [A-Za-z0-9][A-Za-z0-9._-]{0,79}")
    return value


def validate_repeats(repeat_a: int, repeat_b: int, repeat_c: int) -> tuple[int, int, int]:
    values = (repeat_a, repeat_b, repeat_c)
    if any(isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 64 for value in values):
        raise ValueError("repeat_a, repeat_b, and repeat_c must be integers from 1 to 64")
    if math.prod(values) > 4096:
        raise ValueError("Supercell replication product exceeds 4096")
    return values


def validate_crystal_parent_request(
    source: Path, expected_elements: dict[str, int], max_atoms: int
) -> dict[str, Any]:
    if source.suffix.lower() not in {".cif", ".xsd"}:
        raise ValueError("Crystal parent import requires a CIF or XSD source")
    if isinstance(max_atoms, bool) or not isinstance(max_atoms, int) or not 1 <= max_atoms <= 100000:
        raise ValueError("max_atoms must be an integer from 1 to 100000")
    if not isinstance(expected_elements, dict) or not expected_elements:
        raise ValueError("expected_elements must be a non-empty element-to-count object")
    normalized: dict[str, int] = {}
    for element, count in expected_elements.items():
        if not isinstance(element, str) or _ELEMENT.fullmatch(element) is None:
            raise ValueError(f"Invalid element symbol in expected_elements: {element!r}")
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            raise ValueError(f"Expected count for {element} must be a positive integer")
        normalized[element] = count
    expected_atom_count = sum(normalized.values())
    if expected_atom_count > max_atoms:
        raise ValueError(
            f"Expected crystal parent atom count {expected_atom_count} exceeds max_atoms {max_atoms}"
        )
    return {
        "source_path": str(source),
        "source_suffix": source.suffix.lower(),
        "expected_elements": dict(sorted(normalized.items())),
        "expected_atom_count": expected_atom_count,
        "max_atoms": max_atoms,
    }


def build_crystal_parent_import_script() -> str:
    return '''use strict;
use warnings;
use MaterialsScript qw(:all);
my $doc = Documents->Import("{{input.structure}}");
$doc->Export("{{output.structure}}");
$doc->Close;
'''


def validate_crystal_parent_import_result(
    output_path: Path, expected_elements: dict[str, int], max_atoms: int
) -> dict[str, Any]:
    output = inspect_xsd_geometry(output_path)
    errors: list[str] = []
    if output["periodic_dimension"] != 3:
        errors.append("imported crystal parent is not three-dimensionally periodic")
    if output["elements"] != dict(sorted(expected_elements.items())):
        errors.append(
            f"element inventory {output['elements']} != {dict(sorted(expected_elements.items()))}"
        )
    if output["atom_count"] > max_atoms:
        errors.append(f"atom_count {output['atom_count']} exceeds max_atoms {max_atoms}")
    if output["duplicate_fractional_coordinates"] != 0:
        errors.append("imported crystal parent contains duplicate fractional coordinates")
    if errors:
        raise RuntimeError("Crystal parent import post-validation failed: " + "; ".join(errors))
    return {
        "status": "crystal_parent_import_pass",
        "production_released": False,
        "expected_elements": dict(sorted(expected_elements.items())),
        "expected_atom_count": sum(expected_elements.values()),
        "output": output,
    }


def validate_surface_parameters(
    miller_h: int,
    miller_k: int,
    miller_l: int,
    thickness_angstrom: float,
    top_positions: list[float],
    max_candidates: int,
) -> dict[str, Any]:
    miller = (miller_h, miller_k, miller_l)
    if any(isinstance(value, bool) or not isinstance(value, int) or not -20 <= value <= 20 for value in miller):
        raise ValueError("Miller indices must be integers from -20 to 20")
    if miller == (0, 0, 0):
        raise ValueError("Miller index (0,0,0) is invalid")
    if isinstance(thickness_angstrom, bool) or not isinstance(thickness_angstrom, (int, float)):
        raise ValueError("thickness_angstrom must be numeric")
    thickness = float(thickness_angstrom)
    if not math.isfinite(thickness) or not 1.0 <= thickness <= 500.0:
        raise ValueError("thickness_angstrom must be finite and between 1 and 500")
    if isinstance(max_candidates, bool) or not isinstance(max_candidates, int) or not 1 <= max_candidates <= 64:
        raise ValueError("max_candidates must be an integer from 1 to 64")
    if not isinstance(top_positions, list) or not top_positions or len(top_positions) > max_candidates:
        raise ValueError("top_positions must be a non-empty array no longer than max_candidates")
    normalized: list[float] = []
    for item in top_positions:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise ValueError("Each top position must be numeric")
        value = float(item)
        if not math.isfinite(value) or not 0.0 <= value < 1.0:
            raise ValueError("Each top position must be finite and in [0,1)")
        normalized.append(value)
    if len({round(value, 12) for value in normalized}) != len(normalized):
        raise ValueError("top_positions must be unique")
    return {"miller": miller, "thickness_angstrom": thickness, "top_positions": normalized}


def validate_surface_mesh_vectors(
    miller: tuple[int, int, int],
    u_vector: list[int] | None,
    v_vector: list[int] | None,
) -> tuple[tuple[int, int, int], tuple[int, int, int]] | None:
    if u_vector is None and v_vector is None:
        return None
    if u_vector is None or v_vector is None:
        raise ValueError("u_vector and v_vector must be supplied together")

    def normalized(name: str, value: list[int]) -> tuple[int, int, int]:
        if not isinstance(value, list) or len(value) != 3:
            raise ValueError(f"{name} must contain exactly three integers")
        if any(isinstance(item, bool) or not isinstance(item, int) or not -64 <= item <= 64 for item in value):
            raise ValueError(f"{name} entries must be integers from -64 to 64")
        result = tuple(value)
        if result == (0, 0, 0):
            raise ValueError(f"{name} must be nonzero")
        if sum(h * x for h, x in zip(miller, result)) != 0:
            raise ValueError(f"{name} is not in the requested Miller plane")
        return result  # type: ignore[return-value]

    u = normalized("u_vector", u_vector)
    v = normalized("v_vector", v_vector)
    cross = (
        u[1] * v[2] - u[2] * v[1],
        u[2] * v[0] - u[0] * v[2],
        u[0] * v[1] - u[1] * v[0],
    )
    if cross == (0, 0, 0):
        raise ValueError("u_vector and v_vector must not be colinear")
    if max(abs(item) for item in cross) > 4096:
        raise ValueError("surface mesh vector area index exceeds 4096")
    return u, v


def _vector(raw: str | None) -> list[float] | None:
    if not raw:
        return None
    try:
        values = [float(item.strip()) for item in raw.split(",")]
    except ValueError:
        return None
    return values if len(values) == 3 and all(math.isfinite(item) for item in values) else None


def _determinant(a: list[float], b: list[float], c: list[float]) -> float:
    return (
        a[0] * (b[1] * c[2] - b[2] * c[1])
        - a[1] * (b[0] * c[2] - b[2] * c[0])
        + a[2] * (b[0] * c[1] - b[1] * c[0])
    )


def _fractional_key(values: list[float], periodic_dimension: int) -> tuple[float, float, float]:
    normalized: list[float] = []
    for index, value in enumerate(values):
        if index < periodic_dimension:
            wrapped = value - math.floor(value)
            if math.isclose(wrapped, 1.0, abs_tol=1.0e-10) or math.isclose(wrapped, 0.0, abs_tol=1.0e-10):
                wrapped = 0.0
            normalized.append(round(wrapped, 10))
        else:
            normalized.append(round(value, 10))
    return tuple(normalized)  # type: ignore[return-value]


def _unit_cell_inventory(root: ET.Element, independent_atoms: list[ET.Element]) -> dict[str, Any]:
    """Expand asymmetric atoms with SpaceGroup Operators; images are audit-only."""

    space_groups = list(root.iter("SpaceGroup"))
    plane_groups = list(root.iter("PlaneGroup"))
    if space_groups:
        sg = space_groups[0]
        periodic_dimension = 3
    elif plane_groups:
        sg = plane_groups[0]
        periodic_dimension = 2
    else:
        raise ValueError("XSD has no periodic SpaceGroup or PlaneGroup cell")
    raw_operators = sg.get("Operators")
    operator_chunks = raw_operators.split(":") if raw_operators else []
    if not operator_chunks:
        group_name = (sg.get("GroupName") or "").replace(" ", "").upper()
        if sg.get("ITNumber") == "1" or group_name == "P1":
            operator_chunks = ["1,0,0,0,0,1,0,0,0,0,1,0"]
        else:
            raise ValueError("Non-P1 XSD requires explicit SpaceGroup Operators")

    operators: list[list[float]] = []
    for raw in operator_chunks:
        try:
            transform = [float(item.strip()) for item in raw.split(",")]
        except ValueError as exc:
            raise ValueError("SpaceGroup Operators contain a nonnumeric transform") from exc
        if len(transform) != 12 or not all(math.isfinite(item) for item in transform):
            raise ValueError("Each SpaceGroup operator must be a finite 3x4 transform")
        operators.append(transform)

    def transformed(xyz: list[float], transform: list[float]) -> tuple[float, float, float]:
        return _fractional_key([
            transform[0] * xyz[0] + transform[1] * xyz[1] + transform[2] * xyz[2] + transform[3],
            transform[4] * xyz[0] + transform[5] * xyz[1] + transform[6] * xyz[2] + transform[7],
            transform[8] * xyz[0] + transform[9] * xyz[1] + transform[10] * xyz[2] + transform[11],
        ], periodic_dimension)

    def same_site(left: tuple[float, float, float], right: tuple[float, float, float]) -> bool:
        for index, (a, b) in enumerate(zip(left, right)):
            delta = abs(a - b)
            if index < periodic_dimension:
                delta = min(delta, 1.0 - delta)
            if delta > 1.0e-8:
                return False
        return True

    atom_by_id = {atom.get("ID", ""): atom for atom in independent_atoms}
    orbits: dict[str, list[tuple[float, float, float]]] = {}
    sites: list[tuple[tuple[float, float, float], str, str, float]] = []
    for atom in independent_atoms:
        base_id = atom.get("ID", "")
        raw_xyz = atom.get("XYZ")
        xyz = [0.0, 0.0, 0.0] if raw_xyz is None else _vector(raw_xyz)
        if xyz is None:
            raise ValueError(f"Atom {base_id} has invalid XYZ coordinates")
        element = (atom.get("Components") or atom.get("Name") or "unknown").split(",")[0]
        raw_charge = atom.get("FormalCharge")
        charge = 0.0
        if raw_charge:
            numerator, denominator = (raw_charge.split("/", 1) + ["1"])[:2]
            charge = float(numerator) / float(denominator)
        orbit: list[tuple[float, float, float]] = []
        for operator in operators:
            site = transformed(xyz, operator)
            if not any(same_site(site, existing) for existing in orbit):
                orbit.append(site)
        for site in orbit:
            collision = next((entry for entry in sites if same_site(site, entry[0])), None)
            if collision is not None:
                raise ValueError(
                    f"Distinct asymmetric atoms {collision[1]} and {base_id} occupy the same periodic site {site}"
                )
            sites.append((site, base_id, element, charge))
        orbits[base_id] = orbit

    image_count = 0
    for mapping in root.iter("ImageMapping"):
        try:
            image_transform = [float(item.strip()) for item in (mapping.get("Element") or "").split(",")]
        except ValueError as exc:
            raise ValueError("XSD ImageMapping has a nonnumeric Element transform") from exc
        if len(image_transform) != 12 or not all(math.isfinite(item) for item in image_transform):
            raise ValueError("XSD ImageMapping must contain a finite 3x4 Element transform")
        for image in mapping.iter("Atom3d"):
            base_id = image.get("ImageOf")
            if base_id is None:
                continue
            base = atom_by_id.get(base_id)
            if base is None:
                raise ValueError(f"XSD ImageOf references unknown atom ID {base_id}")
            raw_xyz = base.get("XYZ")
            xyz = [0.0, 0.0, 0.0] if raw_xyz is None else _vector(raw_xyz)
            if xyz is None:
                raise ValueError(f"Atom {base_id} has invalid XYZ coordinates")
            image_site = transformed(xyz, image_transform)
            if not any(same_site(image_site, site) for site in orbits[base_id]):
                raise ValueError(f"ImageMapping for atom {base_id} is outside its SpaceGroup operator orbit")
            image_count += 1

    elements: Counter[str] = Counter(value[2] for value in sites)
    return {
        "atom_count": len(sites),
        "elements": dict(sorted(elements.items())),
        "formal_charge_e": sum(value[3] for value in sites),
        "persisted_image_atom_count": image_count,
        "symmetry_operator_count": len(operators),
        "periodic_dimension": periodic_dimension,
    }


def inspect_xsd_geometry(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.suffix.lower() != ".xsd":
        raise ValueError(f"Geology modeling currently requires an XSD input: {path}")
    root = ET.parse(path).getroot()
    # XSD persists symmetry-display images as Atom3d/Bond3d records with
    # ImageOf. They are not independent unit-cell atoms/topology entries.
    atoms = [item for item in root.iter("Atom3d") if item.get("ImageOf") is None]
    bonds = [
        item for item in root.iter()
        if item.tag.rsplit("}", 1)[-1] in {"Bond", "Bond3d"} and item.get("ImageOf") is None
    ]
    if not atoms:
        raise ValueError("XSD contains no Atom3d records")
    ids: list[str] = []
    elements: Counter[str] = Counter()
    coordinates: list[tuple[float, float, float]] = []
    formal_charge = 0.0
    for atom in atoms:
        atom_id = atom.get("ID", "")
        if not atom_id:
            raise ValueError("XSD contains an atom without ID")
        ids.append(atom_id)
        raw_xyz = atom.get("XYZ")
        xyz = [0.0, 0.0, 0.0] if raw_xyz is None else _vector(raw_xyz)
        if xyz is None:
            raise ValueError(f"Atom {atom_id} has invalid XYZ coordinates")
        coordinates.append(tuple(xyz))
        element = (atom.get("Components") or atom.get("Name") or "unknown").split(",")[0]
        elements[element] += 1
        raw_charge = atom.get("FormalCharge")
        if raw_charge:
            numerator, denominator = (raw_charge.split("/", 1) + ["1"])[:2]
            formal_charge += float(numerator) / float(denominator)
    if len(set(ids)) != len(ids):
        raise ValueError("XSD atom IDs are not unique")
    rounded = {tuple(round(value, 12) for value in xyz) for xyz in coordinates}
    if len(rounded) != len(coordinates):
        raise ValueError("XSD contains duplicate fractional atom coordinates")
    space_groups = list(root.iter("SpaceGroup"))
    plane_groups = list(root.iter("PlaneGroup"))
    if not space_groups and not plane_groups:
        raise ValueError("XSD has no periodic SpaceGroup or PlaneGroup cell")
    sg = space_groups[0] if space_groups else plane_groups[0]
    vectors = {name: _vector(sg.get(name)) for name in ("AVector", "BVector", "CVector")}
    if not all(vectors.values()):
        raise ValueError("XSD periodic cell vectors are invalid")
    a, b, c = vectors["AVector"], vectors["BVector"], vectors["CVector"]
    volume = abs(_determinant(a, b, c))
    if not math.isfinite(volume) or volume <= 1.0e-8:
        raise ValueError("XSD periodic cell volume is invalid")
    inventory = _unit_cell_inventory(root, atoms)
    return {
        "path": str(path),
        "sha256": sha256_file(path),
        "atom_count": inventory["atom_count"],
        "asymmetric_atom_count": len(atoms),
        "persisted_image_atom_count": inventory["persisted_image_atom_count"],
        "symmetry_operator_count": inventory["symmetry_operator_count"],
        "periodic_dimension": inventory["periodic_dimension"],
        "bond_count": len(bonds),
        "bond_count_unit_cell_exact": inventory["symmetry_operator_count"] == 1,
        "elements": inventory["elements"],
        "asymmetric_elements": dict(sorted(elements.items())),
        "formal_charge_e": inventory["formal_charge_e"],
        "asymmetric_formal_charge_e": formal_charge,
        "cell_vectors": vectors,
        "cell_volume_A3": volume,
        "duplicate_fractional_coordinates": 0,
    }


def validate_supercell_result(
    input_model: dict[str, Any], output_path: Path, repeats: tuple[int, int, int], max_atoms: int
) -> dict[str, Any]:
    output = inspect_xsd_geometry(output_path)
    factor = math.prod(repeats)
    expected_atoms = input_model["atom_count"] * factor
    if expected_atoms > max_atoms:
        raise ValueError(f"Expected supercell atom count {expected_atoms} exceeds max_atoms {max_atoms}")
    errors: list[str] = []
    if output["atom_count"] != expected_atoms:
        errors.append(f"atom_count {output['atom_count']} != {expected_atoms}")
    if input_model["bond_count"] and not input_model.get("bond_count_unit_cell_exact", False):
        errors.append("input symmetry-represented bonds cannot be audited as exact unit-cell topology")
    elif input_model["bond_count"] and output["bond_count"] != input_model["bond_count"] * factor:
        errors.append("explicit bond count is not the exact replication of the input")
    expected_elements = {key: value * factor for key, value in input_model["elements"].items()}
    if output["elements"] != expected_elements:
        errors.append("element composition is not the exact replication of the input")
    if not math.isclose(output["formal_charge_e"], input_model["formal_charge_e"] * factor, abs_tol=1.0e-8):
        errors.append("formal charge is not conserved under replication")
    for axis, repeat in zip(("AVector", "BVector", "CVector"), repeats):
        expected = [value * repeat for value in input_model["cell_vectors"][axis]]
        observed = output["cell_vectors"][axis]
        if any(not math.isclose(a, b, rel_tol=1.0e-8, abs_tol=1.0e-8) for a, b in zip(observed, expected)):
            errors.append(f"{axis} does not scale by repeat {repeat}")
    if errors:
        raise RuntimeError("Supercell post-validation failed: " + "; ".join(errors))
    return {
        "status": "pass",
        "replication": list(repeats),
        "replication_factor": factor,
        "input": input_model,
        "output": output,
        "expected_atom_count": expected_atoms,
    }


def build_supercell_script(repeats: tuple[int, int, int], periodic_dimension: int = 3) -> str:
    a, b, c = repeats
    if periodic_dimension not in (2, 3):
        raise ValueError("periodic_dimension must be 2 or 3")
    if periodic_dimension == 2 and c != 1:
        raise ValueError("A 2D surface supercell requires repeat_c=1")
    arguments = f"{a}, {b}" if periodic_dimension == 2 else f"{a}, {b}, {c}"
    return f'''use strict;
use warnings;
use MaterialsScript qw(:all);
my $doc = Documents->Import("{{{{input.structure}}}}");
$doc->BuildSuperCell({arguments});
$doc->Export("{{{{output.structure}}}}");
$doc->Close;
'''


def surface_normal_span_angstrom(path: Path) -> float:
    ledger = inspect_p1_atom_ledger(path, (2,))
    vectors = ledger["model"]["cell_vectors"]
    a, b, c = (vectors[key] for key in ("AVector", "BVector", "CVector"))
    c_norm = math.sqrt(sum(value * value for value in c))
    if c_norm <= 1.0e-12:
        raise ValueError("Surface normal vector is degenerate")
    normal = [value / c_norm for value in c]
    projections: list[float] = []
    for atom in ledger["atoms"]:
        f = atom["fractional_xyz"]
        cart = [f[0] * a[i] + f[1] * b[i] + f[2] * c[i] for i in range(3)]
        projections.append(sum(cart[i] * normal[i] for i in range(3)))
    return max(projections) - min(projections)


def build_periodic_slab_cell_script(vacuum_thickness_angstrom: float) -> str:
    if not math.isfinite(vacuum_thickness_angstrom) or not 1.0 <= vacuum_thickness_angstrom <= 10000.0:
        raise ValueError("vacuum_thickness_angstrom must be finite and between 1 and 10000")
    return f'''use strict;
use warnings;
use MaterialsScript qw(:all);
my $doc = Documents->Import("{{{{input.structure}}}}");
$doc->BuildVacuumSlab(Settings(
    VacuumThickness => {vacuum_thickness_angstrom:.12g},
    OrientationStandard => "A along X, B in XY plane"
));
$doc->Export("{{{{output.structure}}}}");
$doc->Close;
'''


def validate_periodic_slab_cell_result(
    input_model: dict[str, Any], output_path: Path, expected_total_c_angstrom: float,
    cell_tolerance_angstrom: float,
) -> dict[str, Any]:
    if not math.isfinite(expected_total_c_angstrom) or expected_total_c_angstrom <= 0:
        raise ValueError("expected_total_c_angstrom must be positive and finite")
    if not math.isfinite(cell_tolerance_angstrom) or not 0 < cell_tolerance_angstrom <= 10:
        raise ValueError("cell_tolerance_angstrom must be in (0,10]")
    output = inspect_xsd_geometry(output_path)
    errors: list[str] = []
    if input_model["periodic_dimension"] != 2 or output["periodic_dimension"] != 3:
        errors.append("periodicity did not change from 2D PlaneGroup to 3D SpaceGroup")
    for key in ("atom_count", "bond_count", "elements"):
        if output[key] != input_model[key]:
            errors.append(f"{key} changed during periodic slab-cell construction")
    def length(vector: list[float]) -> float:
        return math.sqrt(sum(value * value for value in vector))
    for axis in ("AVector", "BVector"):
        if not math.isclose(
            length(output["cell_vectors"][axis]), length(input_model["cell_vectors"][axis]),
            abs_tol=cell_tolerance_angstrom,
        ):
            errors.append(f"{axis} length changed outside tolerance")
    observed_c = length(output["cell_vectors"]["CVector"])
    if not math.isclose(observed_c, expected_total_c_angstrom, abs_tol=cell_tolerance_angstrom):
        errors.append(
            f"CVector length {observed_c:.12g} != expected {expected_total_c_angstrom:.12g} A"
        )
    if errors:
        raise RuntimeError("Periodic slab-cell post-validation failed: " + "; ".join(errors))
    return {
        "status": "periodic_slab_cell_pass",
        "production_released": False,
        "expected_total_c_angstrom": expected_total_c_angstrom,
        "observed_total_c_angstrom": observed_c,
        "cell_tolerance_angstrom": cell_tolerance_angstrom,
        "input": input_model,
        "output": output,
    }


def build_surface_enumeration_script(
    miller: tuple[int, int, int],
    thickness_angstrom: float,
    top_positions: list[float],
    u_vector: tuple[int, int, int] | None = None,
    v_vector: tuple[int, int, int] | None = None,
) -> str:
    h, k, l = miller
    blocks: list[str] = []
    for index, position in enumerate(top_positions):
        blocks.append(
            f'''$cleaver->SetTopPosition({position:.12g});
my $surface_{index} = $cleaver->Cleave;
$surface_{index}->Export("{{{{output.candidate_{index}}}}}");
$surface_{index}->Close;
'''
        )
    define = f"$cleaver->DefineCleave($doc, MillerIndex(H => {h}, K => {k}, L => {l}));"
    if u_vector is not None and v_vector is not None:
        define = (
            f"$cleaver->DefineCleave($doc, MillerIndex(H => {h}, K => {k}, L => {l}), "
            f"Point(X => {u_vector[0]}, Y => {u_vector[1]}, Z => {u_vector[2]}), "
            f"Point(X => {v_vector[0]}, Y => {v_vector[1]}, Z => {v_vector[2]}));"
        )
    return f'''use strict;
use warnings;
use MaterialsScript qw(:all);
my $doc = Documents->Import("{{{{input.structure}}}}");
my $cleaver = Tools->SurfaceBuilder->CleaveSurface;
{define}
$cleaver->SetThickness({thickness_angstrom:.12g}, "Angstrom");
{''.join(blocks)}
$doc->Close;
'''


def validate_surface_candidates(
    paths: list[Path], miller: tuple[int, int, int], thickness: float, positions: list[float]
) -> dict[str, Any]:
    if len(paths) != len(positions):
        raise ValueError("Surface candidate path/position ledger changed")
    candidates: list[dict[str, Any]] = []
    hashes: set[str] = set()
    for index, (path, position) in enumerate(zip(paths, positions)):
        model = inspect_xsd_geometry(path)
        if model["sha256"] in hashes:
            raise RuntimeError("Two requested surface positions produced byte-identical candidates")
        hashes.add(model["sha256"])
        candidates.append(
            {
                "candidate_index": index,
                "top_position": position,
                "status": "candidate_only",
                "production_released": False,
                **model,
            }
        )
    return {
        "status": "candidate_enumeration_pass",
        "production_released": False,
        "miller_index": list(miller),
        "thickness_angstrom": thickness,
        "candidates": candidates,
        "limitations": [
            "No termination is selected as scientifically preferred.",
            "No automatic bond repair, hydroxylation, forcefield assignment, or production release is performed.",
        ],
    }


def _formal_charge(atom: ET.Element) -> float:
    raw = atom.get("FormalCharge")
    if not raw:
        return 0.0
    numerator, denominator = (raw.split("/", 1) + ["1"])[:2]
    value = float(numerator) / float(denominator)
    if not math.isfinite(value):
        raise ValueError("XSD contains a non-finite formal charge")
    return value


def inspect_p1_atom_ledger(path: Path, allowed_dimensions: tuple[int, ...] = (3,)) -> dict[str, Any]:
    model = inspect_xsd_geometry(path)
    if model["periodic_dimension"] not in allowed_dimensions or model["symmetry_operator_count"] != 1:
        dimensions = ",".join(str(value) for value in allowed_dimensions)
        raise ValueError(f"Site-ledger mutations require a P1 XSD in periodic dimension {dimensions}")
    if model["atom_count"] != model["asymmetric_atom_count"]:
        raise ValueError("Site-ledger mutations require every unit-cell atom to be explicit")
    root = ET.parse(path).getroot()
    records: list[dict[str, Any]] = []
    for atom_index, atom in enumerate(item for item in root.iter("Atom3d") if item.get("ImageOf") is None):
        name = atom.get("Name")
        if not name:
            raise ValueError("Every mutable P1 atom must have a Name")
        raw_xyz = atom.get("XYZ")
        xyz = [0.0, 0.0, 0.0] if raw_xyz is None else _vector(raw_xyz)
        if xyz is None:
            raise ValueError(f"Atom {name} must have finite fractional XYZ coordinates")
        records.append(
            {
                "atom_index": atom_index,
                "name": name,
                "element": (atom.get("Components") or "").split(",")[0],
                "formal_charge_e": _formal_charge(atom),
                "formal_charge_explicit": atom.get("FormalCharge") is not None,
                "fractional_xyz": xyz,
            }
        )
    by_name: dict[str, list[dict[str, Any]]] = {}
    for item in records:
        by_name.setdefault(item["name"], []).append(item)
    return {
        "model": model,
        "atoms": records,
        "by_index": {item["atom_index"]: item for item in records},
        "by_name": by_name,
    }


def inspect_explicit_bond_pairs(path: Path) -> Counter[tuple[int, int]]:
    root = ET.parse(path).getroot()
    atoms = [item for item in root.iter("Atom3d") if item.get("ImageOf") is None]
    id_to_index = {atom.get("ID", ""): index for index, atom in enumerate(atoms)}
    if "" in id_to_index:
        raise ValueError("XSD atom bond ledger contains a missing atom ID")
    for image in (item for item in root.iter("Atom3d") if item.get("ImageOf") is not None):
        image_id = image.get("ID", "")
        base_id = image.get("ImageOf", "")
        if not image_id or base_id not in id_to_index:
            raise ValueError("XSD image atom references an unknown base atom")
        id_to_index[image_id] = id_to_index[base_id]
    pairs: Counter[tuple[int, int]] = Counter()
    for bond in (
        item for item in root.iter()
        if item.tag.rsplit("}", 1)[-1] in {"Bond", "Bond3d"} and item.get("ImageOf") is None
    ):
        connects = (bond.get("Connects") or "").split(",")
        if len(connects) != 2 or any(value not in id_to_index for value in connects):
            raise ValueError("XSD bond references an unknown or malformed atom pair")
        pair = tuple(sorted((id_to_index[connects[0]], id_to_index[connects[1]])))
        if pair[0] == pair[1]:
            raise ValueError("XSD contains a self-bond after periodic-image resolution")
        pairs[pair] += 1
    return pairs


def _finite_charge(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be numeric")
    charge = float(value)
    if not math.isfinite(charge) or not -8.0 <= charge <= 8.0:
        raise ValueError(f"{field} must be finite and between -8 and 8 e")
    rational = Fraction(charge).limit_denominator(1024)
    if not math.isclose(float(rational), charge, abs_tol=1.0e-10):
        raise ValueError(f"{field} must be representable as a rational charge with denominator at most 1024")
    return charge


def _rational_parts(value: float) -> tuple[int, int]:
    rational = Fraction(value).limit_denominator(1024)
    return rational.numerator, rational.denominator


def validate_substitution_ledger(path: Path, substitutions: list[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(substitutions, list) or not substitutions or len(substitutions) > 100_000:
        raise ValueError("substitutions must be a non-empty array with at most 100000 entries")
    ledger = inspect_p1_atom_ledger(path)
    normalized: list[dict[str, Any]] = []
    seen: set[int] = set()
    for index, item in enumerate(substitutions):
        if not isinstance(item, dict):
            raise ValueError(f"substitutions[{index}] must be an object")
        atom_index = item.get("atom_index")
        expected_xyz = item.get("expected_fractional_xyz")
        requested_name = item.get("atom_name")
        before = item.get("from_element")
        after = item.get("to_element")
        if isinstance(atom_index, bool) or not isinstance(atom_index, int) or atom_index < 0:
            raise ValueError(f"substitutions[{index}].atom_index must be a nonnegative integer")
        if atom_index in seen:
            raise ValueError(f"Duplicate substitution atom_index: {atom_index}")
        seen.add(atom_index)
        if requested_name is not None and (not isinstance(requested_name, str) or not requested_name):
            raise ValueError(f"substitutions[{index}].atom_name must be a non-empty string when supplied")
        if not isinstance(expected_xyz, list) or len(expected_xyz) != 3 or any(
            isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value))
            for value in expected_xyz
        ):
            raise ValueError(f"substitutions[{index}].expected_fractional_xyz must contain three finite values")
        if not isinstance(before, str) or _ELEMENT.fullmatch(before) is None:
            raise ValueError(f"substitutions[{index}].from_element is invalid")
        if not isinstance(after, str) or _ELEMENT.fullmatch(after) is None or after == before:
            raise ValueError(f"substitutions[{index}].to_element must be a different element symbol")
        from_charge = _finite_charge(item.get("from_formal_charge_e"), f"substitutions[{index}].from_formal_charge_e")
        to_charge = _finite_charge(item.get("to_formal_charge_e"), f"substitutions[{index}].to_formal_charge_e")
        source = ledger["by_index"].get(atom_index)
        if source is None:
            raise ValueError(f"Substitution atom index does not exist: {atom_index}")
        if requested_name is not None and source["name"] != requested_name:
            raise ValueError(f"Substitution name precondition mismatch at index {atom_index}")
        if any(
            not math.isclose(actual, float(expected), abs_tol=1.0e-8)
            for actual, expected in zip(source["fractional_xyz"], expected_xyz)
        ):
            raise ValueError(f"Substitution coordinate precondition mismatch at index {atom_index}")
        if source["element"] != before:
            raise ValueError(f"Substitution chemistry precondition mismatch at index {atom_index}")
        if source["formal_charge_explicit"] and not math.isclose(source["formal_charge_e"], from_charge, abs_tol=1.0e-8):
            raise ValueError(f"Substitution formal-charge precondition mismatch at index {atom_index}")
        normalized.append(
            {
                "atom_index": atom_index,
                "atom_name": source["name"],
                "expected_fractional_xyz": [float(value) for value in expected_xyz],
                "from_element": before,
                "to_element": after,
                "from_formal_charge_e": from_charge,
                "to_formal_charge_e": to_charge,
            }
        )
    return {"input": ledger, "substitutions": normalized}


def _perl_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_substitution_script(substitutions: list[dict[str, Any]]) -> str:
    blocks: list[str] = []
    for item in substitutions:
        name = _perl_string(item["atom_name"])
        atom_index = item["atom_index"]
        before = _perl_string(item["from_element"])
        after = _perl_string(item["to_element"])
        from_charge = item["from_formal_charge_e"]
        to_charge = item["to_formal_charge_e"]
        from_numerator, from_denominator = _rational_parts(from_charge)
        numerator, denominator = _rational_parts(to_charge)
        blocks.append(
            f'''die "Atom index is outside the asymmetric unit" unless $atoms->Count > {atom_index};
my $atom = $doc->AsymmetricUnit->Atoms({atom_index});
die "Name precondition failed at atom index {atom_index}" unless $atom->Name eq {name};
die "Element precondition failed for " . {name} unless $atom->ElementSymbol eq {before};
die "Formal-charge precondition failed for " . {name} unless
    $atom->FormalCharge->Numerator == {from_numerator} && $atom->FormalCharge->Denominator == {from_denominator};
$atom->ElementSymbol = {after};
$atom->FormalCharge->Numerator = {numerator};
$atom->FormalCharge->Denominator = {denominator};
$atom->Name = {name};
'''
        )
    return f'''use strict;
use warnings;
use MaterialsScript qw(:all);
my $doc = Documents->Import("{{{{input.structure}}}}");
my $atoms = $doc->AsymmetricUnit->Atoms;
sub charge_total {{
    my ($document) = @_;
    my $items = $document->AsymmetricUnit->Atoms;
    my $total = 0;
    foreach my $item (@$items) {{
        my $charge = $item->FormalCharge;
        $total += $charge->Numerator / $charge->Denominator;
    }}
    return ($items->Count, $total);
}}
my ($before_count, $before_charge) = charge_total($doc);
{''.join(blocks)}
my ($after_count, $after_charge) = charge_total($doc);
open(my $audit, '>', "{{{{output.charge_audit}}}}") or die $!;
print $audit "stage\\tatom_count\\tformal_charge_e\\n";
print $audit "before\\t$before_count\\t$before_charge\\n";
print $audit "after\\t$after_count\\t$after_charge\\n";
close($audit);
$doc->Export("{{{{output.structure}}}}");
$doc->Close;
'''


def _cell_unchanged(before: dict[str, Any], after: dict[str, Any]) -> bool:
    return all(
        all(math.isclose(x, y, rel_tol=1.0e-8, abs_tol=1.0e-8) for x, y in zip(before[axis], after[axis]))
        for axis in ("AVector", "BVector", "CVector")
    )


def validate_substitution_result(
    input_ledger: dict[str, Any], output_path: Path, substitutions: list[dict[str, Any]],
    charge_audit: dict[str, dict[str, float | int]] | None = None,
) -> dict[str, Any]:
    output = inspect_p1_atom_ledger(output_path)
    before_model = input_ledger["model"]
    after_model = output["model"]
    expected = {item["atom_index"]: item for item in substitutions}
    errors: list[str] = []
    if after_model["atom_count"] != before_model["atom_count"]:
        errors.append("atom count changed")
    if after_model["bond_count"] != before_model["bond_count"]:
        errors.append("bond count changed")
    if not _cell_unchanged(before_model["cell_vectors"], after_model["cell_vectors"]):
        errors.append("cell vectors changed")
    for atom_index, before in input_ledger["by_index"].items():
        after = output["by_index"].get(atom_index)
        if after is None:
            continue
        target = expected.get(atom_index)
        expected_element = target["to_element"] if target else before["element"]
        expected_charge = target["to_formal_charge_e"] if target else before["formal_charge_e"]
        charge_mismatch = charge_audit is None and not math.isclose(after["formal_charge_e"], expected_charge, abs_tol=1.0e-8)
        if after["element"] != expected_element or charge_mismatch:
            errors.append(f"unexpected chemistry at atom index {atom_index}")
        if after["name"] != before["name"]:
            errors.append(f"atom name changed at index {atom_index}")
        if any(not math.isclose(x, y, abs_tol=1.0e-8) for x, y in zip(after["fractional_xyz"], before["fractional_xyz"])):
            errors.append(f"coordinates changed at atom index {atom_index}")
    formal_charge_before = (
        float(charge_audit["before"]["formal_charge_e"]) if charge_audit else before_model["formal_charge_e"]
    )
    formal_charge_after = (
        float(charge_audit["after"]["formal_charge_e"]) if charge_audit else after_model["formal_charge_e"]
    )
    if charge_audit and (
        int(charge_audit["before"]["atom_count"]) != before_model["atom_count"]
        or int(charge_audit["after"]["atom_count"]) != after_model["atom_count"]
    ):
        errors.append("MaterialsScript charge-audit atom counts do not match XSD counts")
    expected_charge = formal_charge_before + sum(
        item["to_formal_charge_e"] - item["from_formal_charge_e"] for item in substitutions
    )
    if not math.isclose(formal_charge_after, expected_charge, abs_tol=1.0e-8):
        errors.append("formal-charge delta does not match substitution ledger")
    if errors:
        raise RuntimeError("Substitution post-validation failed: " + "; ".join(errors))
    audited_output = dict(after_model)
    if charge_audit:
        audited_output["xsd_serialized_formal_charge_e"] = after_model["formal_charge_e"]
        audited_output["formal_charge_e"] = formal_charge_after
        audited_output["asymmetric_formal_charge_e"] = formal_charge_after
        audited_output["formal_charge_audit_source"] = "MaterialsScript runtime ledger"
    return {
        "status": "substitution_geometry_pass",
        "production_released": False,
        "substitution_count": len(substitutions),
        "formal_charge_before_e": formal_charge_before,
        "formal_charge_after_e": formal_charge_after,
        "output": audited_output,
    }


def _fractional_distance(
    left: list[float], right: list[float], cell_vectors: dict[str, list[float]], periodic_dimension: int = 3,
) -> float:
    if periodic_dimension not in (2, 3):
        raise ValueError("periodic_dimension must be 2 or 3")
    delta = [left[index] - right[index] for index in range(3)]
    a, b, c = (cell_vectors[key] for key in ("AVector", "BVector", "CVector"))
    matrix = [[a[row], b[row], c[row]] for row in range(3)]
    det = _determinant(a, b, c)
    if abs(det) <= 1.0e-12:
        raise ValueError("Periodic cell is singular")
    inverse = [
        [(matrix[1][1] * matrix[2][2] - matrix[1][2] * matrix[2][1]) / det,
         (matrix[0][2] * matrix[2][1] - matrix[0][1] * matrix[2][2]) / det,
         (matrix[0][1] * matrix[1][2] - matrix[0][2] * matrix[1][1]) / det],
        [(matrix[1][2] * matrix[2][0] - matrix[1][0] * matrix[2][2]) / det,
         (matrix[0][0] * matrix[2][2] - matrix[0][2] * matrix[2][0]) / det,
         (matrix[0][2] * matrix[1][0] - matrix[0][0] * matrix[1][2]) / det],
        [(matrix[1][0] * matrix[2][1] - matrix[1][1] * matrix[2][0]) / det,
         (matrix[0][1] * matrix[2][0] - matrix[0][0] * matrix[2][1]) / det,
         (matrix[0][0] * matrix[1][1] - matrix[0][1] * matrix[1][0]) / det],
    ]

    def cartesian_norm(translation: tuple[int, int, int]) -> float:
        frac = [delta[index] - translation[index] for index in range(3)]
        cart = [frac[0] * a[index] + frac[1] * b[index] + frac[2] * c[index] for index in range(3)]
        return math.sqrt(sum(value * value for value in cart))

    center = tuple(round(value) if index < periodic_dimension else 0 for index, value in enumerate(delta))
    best = cartesian_norm(center)
    inverse_frobenius = math.sqrt(sum(value * value for row in inverse for value in row))
    radius = inverse_frobenius * best + 1.0e-12
    ranges = [
        range(math.ceil(value - radius), math.floor(value + radius) + 1)
        if index < periodic_dimension else range(0, 1)
        for index, value in enumerate(delta)
    ]
    candidate_count = math.prod(len(values) for values in ranges)
    if candidate_count > 1_000_000:
        raise ValueError("Periodic cell is too ill-conditioned for an exact minimum-image audit")
    for translation in itertools.product(*ranges):
        best = min(best, cartesian_norm(translation))
    return best


def validate_counterion_ledger(
    path: Path,
    placements: list[dict[str, Any]],
    min_framework_distance_angstrom: float,
    min_counterion_distance_angstrom: float,
    max_atoms: int,
) -> dict[str, Any]:
    if not isinstance(placements, list) or not placements or len(placements) > 100_000:
        raise ValueError("placements must be a non-empty array with at most 100000 entries")
    for value, name in (
        (min_framework_distance_angstrom, "min_framework_distance_angstrom"),
        (min_counterion_distance_angstrom, "min_counterion_distance_angstrom"),
    ):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or float(value) <= 0:
            raise ValueError(f"{name} must be a positive finite number")
    if isinstance(max_atoms, bool) or not isinstance(max_atoms, int) or not 1 <= max_atoms <= 10_000_000:
        raise ValueError("max_atoms must be an integer from 1 to 10000000")
    ledger = inspect_p1_atom_ledger(path)
    if ledger["model"]["atom_count"] + len(placements) > max_atoms:
        raise ValueError("Counterion output would exceed max_atoms")
    normalized: list[dict[str, Any]] = []
    names = set(ledger["by_name"])
    for index, item in enumerate(placements):
        if not isinstance(item, dict):
            raise ValueError(f"placements[{index}] must be an object")
        name = item.get("atom_name")
        element = item.get("element")
        xyz = item.get("fractional_xyz")
        if not isinstance(name, str) or _ATOM_NAME.fullmatch(name) is None or name in names:
            raise ValueError(f"placements[{index}].atom_name is invalid or duplicated")
        names.add(name)
        if not isinstance(element, str) or _ELEMENT.fullmatch(element) is None:
            raise ValueError(f"placements[{index}].element is invalid")
        charge = _finite_charge(item.get("formal_charge_e"), f"placements[{index}].formal_charge_e")
        if math.isclose(charge, 0.0, abs_tol=1.0e-12):
            raise ValueError("Counterion formal_charge_e must be nonzero")
        if not isinstance(xyz, list) or len(xyz) != 3 or any(
            isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or not 0 <= float(value) < 1
            for value in xyz
        ):
            raise ValueError(f"placements[{index}].fractional_xyz must contain three finite values in [0,1)")
        normalized.append(
            {"atom_name": name, "element": element, "formal_charge_e": charge, "fractional_xyz": [float(v) for v in xyz]}
        )
    vectors = ledger["model"]["cell_vectors"]
    framework_min = min(
        _fractional_distance(item["fractional_xyz"], atom["fractional_xyz"], vectors)
        for item in normalized for atom in ledger["atoms"]
    )
    ion_min = math.inf
    for left_index, left in enumerate(normalized):
        for right in normalized[left_index + 1:]:
            ion_min = min(ion_min, _fractional_distance(left["fractional_xyz"], right["fractional_xyz"], vectors))
    if framework_min < float(min_framework_distance_angstrom):
        raise ValueError(f"Counterion/framework clearance {framework_min:.6g} A is below the requested minimum")
    if ion_min < float(min_counterion_distance_angstrom):
        raise ValueError(f"Counterion/counterion clearance {ion_min:.6g} A is below the requested minimum")
    return {
        "input": ledger,
        "placements": normalized,
        "minimum_framework_distance_angstrom": framework_min,
        "minimum_counterion_distance_angstrom": None if math.isinf(ion_min) else ion_min,
    }


def build_counterion_script(placements: list[dict[str, Any]]) -> str:
    blocks: list[str] = []
    for item in placements:
        x, y, z = item["fractional_xyz"]
        numerator, denominator = _rational_parts(item["formal_charge_e"])
        blocks.append(
            f'''my $ion = $doc->CreateAtom({_perl_string(item["element"])},
    $doc->FromFractionalPosition(Point(X => {x:.12g}, Y => {y:.12g}, Z => {z:.12g})));
$ion->Name = {_perl_string(item["atom_name"])};
$ion->FormalCharge->Numerator = {numerator};
$ion->FormalCharge->Denominator = {denominator};
'''
        )
    return f'''use strict;
use warnings;
use MaterialsScript qw(:all);
my $doc = Documents->Import("{{{{input.structure}}}}");
sub charge_total {{
    my ($document) = @_;
    my $items = $document->AsymmetricUnit->Atoms;
    my $total = 0;
    foreach my $item (@$items) {{
        my $charge = $item->FormalCharge;
        $total += $charge->Numerator / $charge->Denominator;
    }}
    return ($items->Count, $total);
}}
my ($before_count, $before_charge) = charge_total($doc);
{''.join(blocks)}
my ($after_count, $after_charge) = charge_total($doc);
open(my $audit, '>', "{{{{output.charge_audit}}}}") or die $!;
print $audit "stage\\tatom_count\\tformal_charge_e\\n";
print $audit "before\\t$before_count\\t$before_charge\\n";
print $audit "after\\t$after_count\\t$after_charge\\n";
close($audit);
$doc->Export("{{{{output.structure}}}}");
$doc->Close;
'''


def validate_counterion_result(
    input_ledger: dict[str, Any], output_path: Path, placements: list[dict[str, Any]],
    min_framework_distance_angstrom: float, min_counterion_distance_angstrom: float,
    charge_audit: dict[str, dict[str, float | int]] | None = None,
) -> dict[str, Any]:
    output = inspect_p1_atom_ledger(output_path)
    before_model = input_ledger["model"]
    after_model = output["model"]
    errors: list[str] = []
    if after_model["atom_count"] != before_model["atom_count"] + len(placements):
        errors.append("atom count does not match placement ledger")
    if after_model["bond_count"] != before_model["bond_count"]:
        errors.append("counterion placement unexpectedly changed bonds")
    if not _cell_unchanged(before_model["cell_vectors"], after_model["cell_vectors"]):
        errors.append("cell vectors changed")
    for atom_index, before in input_ledger["by_index"].items():
        after = output["by_index"].get(atom_index)
        charge_mismatch = (
            charge_audit is None and after is not None
            and not math.isclose(after["formal_charge_e"], before["formal_charge_e"], abs_tol=1.0e-8)
        )
        if after is None or after["element"] != before["element"] or charge_mismatch:
            errors.append(f"framework atom changed at index {atom_index}")
        elif after["name"] != before["name"]:
            errors.append(f"framework atom name changed at index {atom_index}")
        elif any(not math.isclose(x, y, abs_tol=1.0e-8) for x, y in zip(after["fractional_xyz"], before["fractional_xyz"])):
            errors.append(f"framework coordinates changed at index {atom_index}")
    vectors = before_model["cell_vectors"]
    for item in placements:
        matches = output["by_name"].get(item["atom_name"], [])
        atom = matches[0] if len(matches) == 1 else None
        charge_mismatch = (
            charge_audit is None and atom is not None
            and not math.isclose(atom["formal_charge_e"], item["formal_charge_e"], abs_tol=1.0e-8)
        )
        if atom is None or atom["element"] != item["element"] or charge_mismatch:
            errors.append(f"counterion chemistry mismatch: {item['atom_name']}")
        elif _fractional_distance(atom["fractional_xyz"], item["fractional_xyz"], vectors) > 1.0e-7:
            errors.append(f"counterion coordinate mismatch: {item['atom_name']}")
    formal_charge_before = (
        float(charge_audit["before"]["formal_charge_e"]) if charge_audit else before_model["formal_charge_e"]
    )
    formal_charge_after = (
        float(charge_audit["after"]["formal_charge_e"]) if charge_audit else after_model["formal_charge_e"]
    )
    if charge_audit and (
        int(charge_audit["before"]["atom_count"]) != before_model["atom_count"]
        or int(charge_audit["after"]["atom_count"]) != after_model["atom_count"]
    ):
        errors.append("MaterialsScript charge-audit atom counts do not match XSD counts")
    expected_charge = formal_charge_before + sum(item["formal_charge_e"] for item in placements)
    if not math.isclose(formal_charge_after, expected_charge, abs_tol=1.0e-8):
        errors.append("formal-charge delta does not match counterion ledger")
    if errors:
        raise RuntimeError("Counterion post-validation failed: " + "; ".join(errors))
    framework_min = min(
        _fractional_distance(item["fractional_xyz"], atom["fractional_xyz"], vectors)
        for item in placements for atom in input_ledger["atoms"]
    )
    ion_min = min(
        (_fractional_distance(left["fractional_xyz"], right["fractional_xyz"], vectors)
         for index, left in enumerate(placements) for right in placements[index + 1:]),
        default=math.inf,
    )
    if framework_min < min_framework_distance_angstrom or ion_min < min_counterion_distance_angstrom:
        raise RuntimeError("Counterion post-validation failed: clearance gate regressed")
    audited_output = dict(after_model)
    if charge_audit:
        audited_output["xsd_serialized_formal_charge_e"] = after_model["formal_charge_e"]
        audited_output["formal_charge_e"] = formal_charge_after
        audited_output["asymmetric_formal_charge_e"] = formal_charge_after
        audited_output["formal_charge_audit_source"] = "MaterialsScript runtime ledger"
    return {
        "status": "counterion_geometry_pass",
        "production_released": False,
        "counterion_count": len(placements),
        "formal_charge_before_e": formal_charge_before,
        "formal_charge_after_e": formal_charge_after,
        "minimum_framework_distance_angstrom": framework_min,
        "minimum_counterion_distance_angstrom": None if math.isinf(ion_min) else ion_min,
        "output": audited_output,
    }


def validate_hydroxylation_ledger(
    path: Path,
    sites: list[dict[str, Any]],
    min_oh_bond_length_angstrom: float,
    max_oh_bond_length_angstrom: float,
    min_nonbonded_distance_angstrom: float,
    max_atoms: int,
) -> dict[str, Any]:
    if not isinstance(sites, list) or not sites or len(sites) > 100_000:
        raise ValueError("sites must be a non-empty array with at most 100000 entries")
    lengths = (min_oh_bond_length_angstrom, max_oh_bond_length_angstrom)
    if any(
        isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value))
        for value in lengths
    ) or not 0.1 <= float(lengths[0]) < float(lengths[1]) <= 5.0:
        raise ValueError("O-H bond limits must be finite, ordered, and within 0.1 to 5 A")
    if (
        isinstance(min_nonbonded_distance_angstrom, bool)
        or not isinstance(min_nonbonded_distance_angstrom, (int, float))
        or not math.isfinite(float(min_nonbonded_distance_angstrom))
        or not 0.1 <= float(min_nonbonded_distance_angstrom) <= 5.0
    ):
        raise ValueError("min_nonbonded_distance_angstrom must be finite and within 0.1 to 5 A")
    if isinstance(max_atoms, bool) or not isinstance(max_atoms, int) or not 1 <= max_atoms <= 10_000_000:
        raise ValueError("max_atoms must be an integer from 1 to 10000000")
    ledger = inspect_p1_atom_ledger(path, (2,))
    if ledger["model"]["atom_count"] + len(sites) > max_atoms:
        raise ValueError("Hydroxylated output would exceed max_atoms")
    bonds = inspect_explicit_bond_pairs(path)
    coordination: dict[int, list[int]] = {index: [] for index in ledger["by_index"]}
    for (left, right), count in bonds.items():
        coordination[left].extend([right] * count)
        coordination[right].extend([left] * count)
    existing_names = set(ledger["by_name"])
    new_names: set[str] = set()
    seen_oxygen: set[int] = set()
    normalized: list[dict[str, Any]] = []
    vectors = ledger["model"]["cell_vectors"]
    for index, item in enumerate(sites):
        if not isinstance(item, dict):
            raise ValueError(f"sites[{index}] must be an object")
        oxygen_index = item.get("oxygen_atom_index")
        oxygen_name = item.get("oxygen_atom_name")
        expected_xyz = item.get("expected_oxygen_fractional_xyz")
        hydrogen_name = item.get("hydrogen_name")
        hydrogen_xyz = item.get("hydrogen_fractional_xyz")
        surface_side = item.get("surface_side")
        if isinstance(oxygen_index, bool) or not isinstance(oxygen_index, int) or oxygen_index < 0:
            raise ValueError(f"sites[{index}].oxygen_atom_index must be a nonnegative integer")
        if oxygen_index in seen_oxygen:
            raise ValueError(f"Duplicate hydroxylation oxygen_atom_index: {oxygen_index}")
        seen_oxygen.add(oxygen_index)
        oxygen = ledger["by_index"].get(oxygen_index)
        if oxygen is None or oxygen["element"] != "O":
            raise ValueError(f"Hydroxylation anchor {oxygen_index} is not an explicit oxygen atom")
        if not isinstance(oxygen_name, str) or oxygen_name != oxygen["name"]:
            raise ValueError(f"Hydroxylation oxygen name precondition mismatch at index {oxygen_index}")
        if not isinstance(expected_xyz, list) or len(expected_xyz) != 3 or any(
            isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value))
            for value in expected_xyz
        ):
            raise ValueError(f"sites[{index}].expected_oxygen_fractional_xyz is invalid")
        if any(
            not math.isclose(actual, float(expected), abs_tol=1.0e-8)
            for actual, expected in zip(oxygen["fractional_xyz"], expected_xyz)
        ):
            raise ValueError(f"Hydroxylation oxygen coordinate precondition mismatch at index {oxygen_index}")
        neighbors = coordination[oxygen_index]
        if len(neighbors) != 1 or ledger["by_index"][neighbors[0]]["element"] != "Si":
            raise ValueError(f"Hydroxylation oxygen {oxygen_index} must be singly coordinated to exactly one Si")
        if not isinstance(hydrogen_name, str) or _ATOM_NAME.fullmatch(hydrogen_name) is None:
            raise ValueError(f"sites[{index}].hydrogen_name is invalid")
        if hydrogen_name in existing_names or hydrogen_name in new_names:
            raise ValueError(f"Hydroxylation hydrogen name is not unique: {hydrogen_name}")
        new_names.add(hydrogen_name)
        if not isinstance(hydrogen_xyz, list) or len(hydrogen_xyz) != 3 or any(
            isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value))
            for value in hydrogen_xyz
        ) or any(not 0.0 <= float(hydrogen_xyz[axis]) < 1.0 for axis in (0, 1)):
            raise ValueError(f"sites[{index}].hydrogen_fractional_xyz must have periodic x/y in [0,1) and finite z")
        if surface_side not in {"top", "bottom"}:
            raise ValueError(f"sites[{index}].surface_side must be top or bottom")
        from_charge = _finite_charge(item.get("oxygen_from_formal_charge_e"), f"sites[{index}].oxygen_from_formal_charge_e")
        to_charge = _finite_charge(item.get("oxygen_to_formal_charge_e"), f"sites[{index}].oxygen_to_formal_charge_e")
        hydrogen_charge = _finite_charge(item.get("hydrogen_formal_charge_e"), f"sites[{index}].hydrogen_formal_charge_e")
        if oxygen["formal_charge_explicit"] and not math.isclose(oxygen["formal_charge_e"], from_charge, abs_tol=1.0e-8):
            raise ValueError(f"Hydroxylation oxygen formal-charge precondition mismatch at index {oxygen_index}")
        h_xyz = [float(value) for value in hydrogen_xyz]
        oh_length = _fractional_distance(oxygen["fractional_xyz"], h_xyz, vectors, 2)
        if not float(min_oh_bond_length_angstrom) <= oh_length <= float(max_oh_bond_length_angstrom):
            raise ValueError(f"Hydroxylation O-H distance {oh_length:.6g} A is outside the requested bond gate")
        nonbonded = min(
            (_fractional_distance(h_xyz, atom["fractional_xyz"], vectors, 2)
             for atom in ledger["atoms"] if atom["atom_index"] != oxygen_index),
            default=math.inf,
        )
        if nonbonded < float(min_nonbonded_distance_angstrom):
            raise ValueError(f"Hydroxylation H/framework clearance {nonbonded:.6g} A is below the requested minimum")
        normalized.append({
            "oxygen_atom_index": oxygen_index,
            "oxygen_atom_name": oxygen_name,
            "expected_oxygen_fractional_xyz": [float(value) for value in expected_xyz],
            "oxygen_from_formal_charge_e": from_charge,
            "oxygen_to_formal_charge_e": to_charge,
            "hydrogen_name": hydrogen_name,
            "hydrogen_fractional_xyz": h_xyz,
            "hydrogen_formal_charge_e": hydrogen_charge,
            "surface_side": surface_side,
            "oh_bond_length_angstrom": oh_length,
            "minimum_h_framework_nonbonded_distance_angstrom": nonbonded,
        })
    for left_index, left in enumerate(normalized):
        for right in normalized[left_index + 1:]:
            distance = _fractional_distance(
                left["hydrogen_fractional_xyz"], right["hydrogen_fractional_xyz"], vectors, 2
            )
            if distance < float(min_nonbonded_distance_angstrom):
                raise ValueError(f"Hydroxylation H/H clearance {distance:.6g} A is below the requested minimum")
    return {"input": ledger, "input_bonds": bonds, "sites": normalized}


def build_hydroxylation_script(sites: list[dict[str, Any]]) -> str:
    blocks: list[str] = []
    for index, item in enumerate(sites):
        from_num, from_den = _rational_parts(item["oxygen_from_formal_charge_e"])
        to_num, to_den = _rational_parts(item["oxygen_to_formal_charge_e"])
        h_num, h_den = _rational_parts(item["hydrogen_formal_charge_e"])
        x, y, z = item["hydrogen_fractional_xyz"]
        oxygen_index = item["oxygen_atom_index"]
        oxygen_name = _perl_string(item["oxygen_atom_name"])
        hydrogen_name = _perl_string(item["hydrogen_name"])
        blocks.append(f'''die "Oxygen index is outside the asymmetric unit" unless $doc->AsymmetricUnit->Atoms->Count > {oxygen_index};
my $oxygen_{index} = $doc->AsymmetricUnit->Atoms({oxygen_index});
die "Oxygen name precondition failed" unless $oxygen_{index}->Name eq {oxygen_name};
die "Oxygen element precondition failed" unless $oxygen_{index}->ElementSymbol eq "O";
my $oxygen_charge_num_{index} = $oxygen_{index}->FormalCharge->Numerator;
my $oxygen_charge_den_{index} = $oxygen_{index}->FormalCharge->Denominator;
die "Oxygen formal-charge precondition failed: actual=$oxygen_charge_num_{index}/$oxygen_charge_den_{index} expected={from_num}/{from_den}" unless
    $oxygen_charge_num_{index} == {from_num} && $oxygen_charge_den_{index} == {from_den};
my $oxygen_charge_value_{index} = $oxygen_{index}->FormalCharge;
$oxygen_charge_value_{index}->Numerator = {to_num};
$oxygen_charge_value_{index}->Denominator = {to_den};
$oxygen_{index}->FormalCharge = $oxygen_charge_value_{index};
my $hydrogen_{index} = $doc->CreateAtom("H",
    $doc->FromFractionalPosition(Point(X => {x:.12g}, Y => {y:.12g}, Z => {z:.12g})));
$hydrogen_{index}->Name = {hydrogen_name};
my $hydrogen_charge_value_{index} = $hydrogen_{index}->FormalCharge;
$hydrogen_charge_value_{index}->Numerator = {h_num};
$hydrogen_charge_value_{index}->Denominator = {h_den};
$hydrogen_{index}->FormalCharge = $hydrogen_charge_value_{index};
$doc->CreateBond($oxygen_{index}, $hydrogen_{index}, "Single");
''')
    return f'''use strict;
use warnings;
use MaterialsScript qw(:all);
my $doc = Documents->Import("{{{{input.structure}}}}");
sub charge_total {{
    my ($document) = @_;
    my $items = $document->AsymmetricUnit->Atoms;
    my $total = 0;
    foreach my $item (@$items) {{
        my $charge = $item->FormalCharge;
        $total += $charge->Numerator / $charge->Denominator;
    }}
    return ($items->Count, $total);
}}
my ($before_count, $before_charge) = charge_total($doc);
{''.join(blocks)}
my ($after_count, $after_charge) = charge_total($doc);
open(my $audit, '>', "{{{{output.charge_audit}}}}") or die $!;
print $audit "stage\\tatom_count\\tformal_charge_e\\n";
print $audit "before\\t$before_count\\t$before_charge\\n";
print $audit "after\\t$after_count\\t$after_charge\\n";
close($audit);
$doc->Export("{{{{output.structure}}}}");
$doc->Close;
'''


def _surface_area_A2(cell_vectors: dict[str, list[float]]) -> float:
    a, b = cell_vectors["AVector"], cell_vectors["BVector"]
    cross = [
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    ]
    area = math.sqrt(sum(value * value for value in cross))
    if not math.isfinite(area) or area <= 1.0e-8:
        raise ValueError("Surface lateral area is invalid")
    return area


def validate_hydroxylation_result(
    input_ledger: dict[str, Any], input_bonds: Counter[tuple[int, int]], output_path: Path,
    sites: list[dict[str, Any]], min_nonbonded_distance_angstrom: float,
    charge_audit: dict[str, dict[str, float | int]],
    required_final_formal_charge_e: float | None = None,
) -> dict[str, Any]:
    output = inspect_p1_atom_ledger(output_path, (2,))
    output_bonds = inspect_explicit_bond_pairs(output_path)
    before_model = input_ledger["model"]
    after_model = output["model"]
    errors: list[str] = []
    if after_model["atom_count"] != before_model["atom_count"] + len(sites):
        errors.append("atom count does not match hydroxylation ledger")
    if sum(output_bonds.values()) != sum(input_bonds.values()) + len(sites):
        errors.append("bond count does not match hydroxylation ledger")
    if not _cell_unchanged(before_model["cell_vectors"], after_model["cell_vectors"]):
        errors.append("cell vectors changed")
    for atom_index, before in input_ledger["by_index"].items():
        after = output["by_index"].get(atom_index)
        if after is None or after["name"] != before["name"] or after["element"] != before["element"]:
            errors.append(f"framework atom identity changed at index {atom_index}")
        elif any(not math.isclose(x, y, abs_tol=1.0e-8) for x, y in zip(after["fractional_xyz"], before["fractional_xyz"])):
            errors.append(f"framework coordinates changed at index {atom_index}")
    vectors = before_model["cell_vectors"]
    requested_bonds: Counter[tuple[int, int]] = Counter()
    h_records: list[dict[str, Any]] = []
    for item in sites:
        matches = output["by_name"].get(item["hydrogen_name"], [])
        hydrogen = matches[0] if len(matches) == 1 else None
        if hydrogen is None or hydrogen["element"] != "H":
            errors.append(f"hydroxyl hydrogen identity mismatch: {item['hydrogen_name']}")
            continue
        if _fractional_distance(hydrogen["fractional_xyz"], item["hydrogen_fractional_xyz"], vectors, 2) > 1.0e-7:
            errors.append(f"hydroxyl hydrogen coordinate mismatch: {item['hydrogen_name']}")
        requested_bonds[tuple(sorted((item["oxygen_atom_index"], hydrogen["atom_index"])))] += 1
        h_records.append(hydrogen)
    new_bonds = output_bonds - input_bonds
    if new_bonds != requested_bonds:
        errors.append("new bond ledger does not exactly match requested O-H bonds")
    if any(output_bonds[pair] < count for pair, count in input_bonds.items()):
        errors.append("an input framework bond was removed")
    formal_charge_before = float(charge_audit["before"]["formal_charge_e"])
    formal_charge_after = float(charge_audit["after"]["formal_charge_e"])
    if (
        int(charge_audit["before"]["atom_count"]) != before_model["atom_count"]
        or int(charge_audit["after"]["atom_count"]) != after_model["atom_count"]
    ):
        errors.append("MaterialsScript charge-audit atom counts do not match XSD counts")
    expected_charge = formal_charge_before + sum(
        item["oxygen_to_formal_charge_e"] - item["oxygen_from_formal_charge_e"]
        + item["hydrogen_formal_charge_e"] for item in sites
    )
    if not math.isclose(formal_charge_after, expected_charge, abs_tol=1.0e-8):
        errors.append("formal-charge delta does not match hydroxylation ledger")
    if required_final_formal_charge_e is not None:
        required_charge = _finite_charge(
            required_final_formal_charge_e, "required_final_formal_charge_e"
        )
        if not math.isclose(formal_charge_after, required_charge, abs_tol=1.0e-8):
            errors.append(
                f"final formal charge {formal_charge_after:.12g} != required {required_charge:.12g} e"
            )
    min_nonbonded = math.inf
    for hydrogen in h_records:
        bonded = next((pair for pair in requested_bonds if hydrogen["atom_index"] in pair), tuple())
        for atom in output["atoms"]:
            if atom["atom_index"] == hydrogen["atom_index"] or atom["atom_index"] in bonded:
                continue
            min_nonbonded = min(
                min_nonbonded,
                _fractional_distance(hydrogen["fractional_xyz"], atom["fractional_xyz"], vectors, 2),
            )
    if min_nonbonded < min_nonbonded_distance_angstrom:
        errors.append("hydroxylation nonbonded clearance gate regressed")
    if errors:
        raise RuntimeError("Hydroxylation post-validation failed: " + "; ".join(errors))
    area = _surface_area_A2(vectors)
    side_counts = Counter(item["surface_side"] for item in sites)
    audited_output = dict(after_model)
    audited_output["xsd_serialized_formal_charge_e"] = after_model["formal_charge_e"]
    audited_output["formal_charge_e"] = formal_charge_after
    audited_output["asymmetric_formal_charge_e"] = formal_charge_after
    audited_output["formal_charge_audit_source"] = "MaterialsScript runtime ledger"
    return {
        "status": "hydroxylation_geometry_pass",
        "production_released": False,
        "hydroxyl_count": len(sites),
        "surface_area_A2": area,
        "surface_side_counts": dict(sorted(side_counts.items())),
        "candidate_silanol_density_OH_nm2": {
            side: count * 100.0 / area for side, count in sorted(side_counts.items())
        },
        "formal_charge_before_e": formal_charge_before,
        "formal_charge_after_e": formal_charge_after,
        "required_final_formal_charge_e": required_final_formal_charge_e,
        "minimum_h_nonbonded_distance_angstrom": None if math.isinf(min_nonbonded) else min_nonbonded,
        "output": audited_output,
    }


def parse_charge_audit(path: Path) -> dict[str, dict[str, float | int]]:
    if not path.is_file():
        raise ValueError("MaterialsScript charge audit is missing")
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] != "stage\tatom_count\tformal_charge_e":
        raise ValueError("MaterialsScript charge audit header is invalid")
    result: dict[str, dict[str, float | int]] = {}
    for line in lines[1:]:
        parts = line.split("\t")
        if len(parts) != 3 or parts[0] not in {"before", "after"} or parts[0] in result:
            raise ValueError("MaterialsScript charge audit row is invalid")
        count = int(parts[1])
        charge = float(parts[2])
        if count < 0 or not math.isfinite(charge):
            raise ValueError("MaterialsScript charge audit contains invalid values")
        result[parts[0]] = {"atom_count": count, "formal_charge_e": charge}
    if set(result) != {"before", "after"}:
        raise ValueError("MaterialsScript charge audit must contain before and after rows")
    return result
