from __future__ import annotations

from collections import Counter
import math
from pathlib import Path
from typing import Any

from .geology_modeling import inspect_p1_atom_ledger, inspect_xsd_geometry


SPCE_OH_ANGSTROM = 1.0
SPCE_HOH_DEGREES = 109.47


def _length(vector: list[float]) -> float:
    return math.sqrt(sum(value * value for value in vector))


def _dot(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right))


def periodic_orthorhombic_frame(path: Path) -> dict[str, Any]:
    ledger = inspect_p1_atom_ledger(path, (3,))
    vectors = ledger["model"]["cell_vectors"]
    ordered = [vectors[name] for name in ("AVector", "BVector", "CVector")]
    lengths = [_length(vector) for vector in ordered]
    if any(length <= 1.0e-8 for length in lengths):
        raise ValueError("Periodic packing requires three nondegenerate cell vectors")
    for left in range(3):
        for right in range(left + 1, 3):
            cosine = abs(_dot(ordered[left], ordered[right]) / (lengths[left] * lengths[right]))
            if cosine > 1.0e-8:
                raise ValueError("Packmol packing currently requires an orthorhombic periodic cell")

    local_atoms: list[dict[str, Any]] = []
    for atom in ledger["atoms"]:
        fractional = atom["fractional_xyz"]
        local_atoms.append(
            {
                **atom,
                "local_xyz": [
                    (fractional[index] - math.floor(fractional[index])) * lengths[index]
                    for index in range(3)
                ],
            }
        )

    z_values = sorted({round(atom["local_xyz"][2], 12) for atom in local_atoms})
    if not z_values:
        raise ValueError("Periodic packing input contains no framework atoms")
    candidates: list[tuple[float, float, float]] = []
    for index, start in enumerate(z_values):
        end = z_values[(index + 1) % len(z_values)]
        if index == len(z_values) - 1:
            end += lengths[2]
        candidates.append((end - start, start, end))
    gap, gap_start, gap_end = max(candidates, key=lambda item: item[0])
    z_shift = -(gap_end % lengths[2])
    for atom in local_atoms:
        atom["local_xyz"][2] = (atom["local_xyz"][2] + z_shift) % lengths[2]

    framework_span = lengths[2] - gap
    return {
        "ledger": ledger,
        "lengths_angstrom": lengths,
        "local_atoms": local_atoms,
        "normal_gap_angstrom": gap,
        "framework_normal_span_angstrom": framework_span,
        "normal_shift_angstrom": z_shift,
        "largest_gap_original": {"start_angstrom": gap_start, "end_angstrom": gap_end},
    }


def validate_aqueous_nacl_request(
    frame: dict[str, Any], *, water_count: int, sodium_count: int, chloride_count: int,
    packmol_tolerance_angstrom: float, normal_boundary_clearance_angstrom: float,
    random_seed: int, max_total_atoms: int, required_final_formal_charge_e: float,
) -> dict[str, Any]:
    counts = {
        "water_count": water_count,
        "sodium_count": sodium_count,
        "chloride_count": chloride_count,
    }
    for name, value in counts.items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{name} must be a nonnegative integer")
    if water_count < 1:
        raise ValueError("water_count must be at least 1")
    if water_count > 1_000_000 or sodium_count > 1_000_000 or chloride_count > 1_000_000:
        raise ValueError("Each requested molecule count must not exceed 1000000")
    if isinstance(random_seed, bool) or not isinstance(random_seed, int) or not 1 <= random_seed <= 2_147_483_647:
        raise ValueError("random_seed must be an integer from 1 to 2147483647")
    for name, value, lower, upper in (
        ("packmol_tolerance_angstrom", packmol_tolerance_angstrom, 0.5, 10.0),
        ("normal_boundary_clearance_angstrom", normal_boundary_clearance_angstrom, 0.0, 20.0),
    ):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise ValueError(f"{name} must be finite and numeric")
        if not lower <= float(value) <= upper:
            raise ValueError(f"{name} must be between {lower} and {upper}")
    if isinstance(max_total_atoms, bool) or not isinstance(max_total_atoms, int) or max_total_atoms < 1:
        raise ValueError("max_total_atoms must be a positive integer")
    if (
        isinstance(required_final_formal_charge_e, bool)
        or not isinstance(required_final_formal_charge_e, (int, float))
        or not math.isfinite(float(required_final_formal_charge_e))
    ):
        raise ValueError("required_final_formal_charge_e must be finite and numeric")

    input_model = frame["ledger"]["model"]
    fluid_atoms = 3 * water_count + sodium_count + chloride_count
    expected_total_atoms = input_model["atom_count"] + fluid_atoms
    if expected_total_atoms > max_total_atoms:
        raise ValueError(
            f"Expected packed atom count {expected_total_atoms} exceeds max_total_atoms {max_total_atoms}"
        )
    expected_charge = input_model["formal_charge_e"] + sodium_count - chloride_count
    if not math.isclose(expected_charge, float(required_final_formal_charge_e), abs_tol=1.0e-10):
        raise ValueError(
            f"Requested composition gives formal charge {expected_charge:.12g} e, not required "
            f"{float(required_final_formal_charge_e):.12g} e"
        )
    z_lower = frame["framework_normal_span_angstrom"] + float(normal_boundary_clearance_angstrom)
    z_upper = frame["lengths_angstrom"][2] - float(normal_boundary_clearance_angstrom)
    if z_upper - z_lower < 2.0 * float(packmol_tolerance_angstrom):
        raise ValueError("Normal packing region is too narrow for the requested clearances")
    return {
        **counts,
        "fluid_atom_count": fluid_atoms,
        "expected_total_atoms": expected_total_atoms,
        "expected_total_bonds": input_model["bond_count"] + 2 * water_count,
        "expected_final_formal_charge_e": expected_charge,
        "packmol_tolerance_angstrom": float(packmol_tolerance_angstrom),
        "normal_boundary_clearance_angstrom": float(normal_boundary_clearance_angstrom),
        "packing_region_angstrom": {
            "x": [0.0, frame["lengths_angstrom"][0]],
            "y": [0.0, frame["lengths_angstrom"][1]],
            "z": [z_lower, z_upper],
        },
        "random_seed": random_seed,
        "max_total_atoms": max_total_atoms,
    }


def xyz_text(atoms: list[tuple[str, float, float, float]], comment: str) -> str:
    lines = [str(len(atoms)), comment]
    lines.extend(f"{element} {x:.12f} {y:.12f} {z:.12f}" for element, x, y, z in atoms)
    return "\n".join(lines) + "\n"


def spce_template_xyz() -> str:
    theta = math.radians(SPCE_HOH_DEGREES)
    return xyz_text(
        [
            ("O", 0.0, 0.0, 0.0),
            ("H", SPCE_OH_ANGSTROM, 0.0, 0.0),
            ("H", SPCE_OH_ANGSTROM * math.cos(theta), SPCE_OH_ANGSTROM * math.sin(theta), 0.0),
        ],
        "canonical SPC/E geometry: rOH=1.0 A, angleHOH=109.47 deg",
    )


def packmol_input_text(
    *, lengths: list[float], region: dict[str, list[float]], tolerance: float, seed: int,
    water_count: int, sodium_count: int, chloride_count: int,
) -> str:
    lines = [
        f"tolerance {tolerance:.12g}",
        "filetype xyz",
        "output packed.xyz",
        f"seed {seed}",
        "nloop 1000",
        f"pbc 0.0 0.0 0.0 {lengths[0]:.12g} {lengths[1]:.12g} {lengths[2]:.12g}",
        "",
        "structure framework.xyz",
        "  number 1",
        "  fixed 0.0 0.0 0.0 0.0 0.0 0.0",
        "end structure",
    ]
    specs = (("spce_water.xyz", water_count), ("sodium.xyz", sodium_count), ("chloride.xyz", chloride_count))
    for filename, count in specs:
        if count == 0:
            continue
        lines.extend(
            [
                "",
                f"structure {filename}",
                f"  number {count}",
                (
                    "  inside box "
                    f"{region['x'][0]:.12g} {region['y'][0]:.12g} {region['z'][0]:.12g} "
                    f"{region['x'][1]:.12g} {region['y'][1]:.12g} {region['z'][1]:.12g}"
                ),
                "end structure",
            ]
        )
    return "\n".join(lines) + "\n"


def parse_xyz(path: Path) -> list[dict[str, Any]]:
    lines = path.read_text(encoding="ascii", errors="strict").splitlines()
    if len(lines) < 2:
        raise RuntimeError("Packmol XYZ output is incomplete")
    try:
        declared = int(lines[0].strip())
    except ValueError as exc:
        raise RuntimeError("Packmol XYZ output has an invalid atom count") from exc
    records: list[dict[str, Any]] = []
    for index, line in enumerate(lines[2:], start=1):
        if not line.strip():
            continue
        fields = line.split()
        if len(fields) < 4:
            raise RuntimeError(f"Packmol XYZ atom line {index} is malformed")
        try:
            xyz = [float(value) for value in fields[1:4]]
        except ValueError as exc:
            raise RuntimeError(f"Packmol XYZ atom line {index} contains invalid coordinates") from exc
        if not all(math.isfinite(value) for value in xyz):
            raise RuntimeError("Packmol XYZ output contains non-finite coordinates")
        records.append({"element": fields[0], "local_xyz": xyz})
    if len(records) != declared:
        raise RuntimeError(f"Packmol XYZ contains {len(records)} atoms, expected {declared}")
    return records


def audit_packed_xyz(
    records: list[dict[str, Any]], frame: dict[str, Any], request: dict[str, Any]
) -> dict[str, Any]:
    framework_count = frame["ledger"]["model"]["atom_count"]
    expected_elements = [atom["element"] for atom in frame["local_atoms"]]
    expected_elements += [item for _ in range(request["water_count"]) for item in ("O", "H", "H")]
    expected_elements += ["Na"] * request["sodium_count"] + ["Cl"] * request["chloride_count"]
    if [item["element"] for item in records] != expected_elements:
        raise RuntimeError("Packmol output atom ordering or element inventory changed")
    for expected, observed in zip(frame["local_atoms"], records[:framework_count]):
        if any(
            not math.isclose(actual, target, abs_tol=1.0e-6)
            for actual, target in zip(observed["local_xyz"], expected["local_xyz"])
        ):
            raise RuntimeError("Packmol moved a fixed framework atom")

    molecule_ids = [-1] * framework_count
    next_molecule = 0
    for _ in range(request["water_count"]):
        molecule_ids.extend([next_molecule] * 3)
        next_molecule += 1
    for _ in range(request["sodium_count"] + request["chloride_count"]):
        molecule_ids.append(next_molecule)
        next_molecule += 1

    lengths = frame["lengths_angstrom"]
    cutoff = max(request["packmol_tolerance_angstrom"] + 0.1, 2.5)
    bins = [max(1, int(length / cutoff)) for length in lengths]
    buckets: dict[tuple[int, int, int], list[int]] = {}
    for index, record in enumerate(records):
        key = tuple(
            min(bins[axis] - 1, int((record["local_xyz"][axis] % lengths[axis]) / lengths[axis] * bins[axis]))
            for axis in range(3)
        )
        buckets.setdefault(key, []).append(index)

    minimum = math.inf
    framework_fluid = math.inf
    fluid_fluid = math.inf
    for left, record in enumerate(records):
        key = tuple(
            min(bins[axis] - 1, int((record["local_xyz"][axis] % lengths[axis]) / lengths[axis] * bins[axis]))
            for axis in range(3)
        )
        neighbor_keys = {
            tuple((key[axis] + delta[axis]) % bins[axis] for axis in range(3))
            for delta in (
                (dx, dy, dz) for dx in (-1, 0, 1) for dy in (-1, 0, 1) for dz in (-1, 0, 1)
            )
        }
        for neighbor_key in neighbor_keys:
            for right in buckets.get(neighbor_key, []):
                if right <= left or (molecule_ids[left] == -1 and molecule_ids[right] == -1):
                    continue
                if molecule_ids[left] >= 0 and molecule_ids[left] == molecule_ids[right]:
                    continue
                distance_squared = 0.0
                for axis in range(3):
                    delta = records[right]["local_xyz"][axis] - record["local_xyz"][axis]
                    delta -= round(delta / lengths[axis]) * lengths[axis]
                    distance_squared += delta * delta
                distance = math.sqrt(distance_squared)
                minimum = min(minimum, distance)
                if molecule_ids[left] == -1 or molecule_ids[right] == -1:
                    framework_fluid = min(framework_fluid, distance)
                else:
                    fluid_fluid = min(fluid_fluid, distance)
    if not math.isfinite(minimum):
        raise RuntimeError("Could not determine a packed intermolecular minimum distance")
    tolerance = request["packmol_tolerance_angstrom"]
    if minimum < tolerance - 1.0e-3:
        raise RuntimeError(
            f"Packed periodic intermolecular clearance {minimum:.12g} A is below tolerance {tolerance:.12g} A"
        )
    return {
        "status": "packmol_periodic_distance_pass",
        "minimum_inter_molecular_distance_angstrom": minimum,
        "minimum_framework_fluid_distance_angstrom": framework_fluid,
        "minimum_fluid_fluid_distance_angstrom": fluid_fluid,
        "molecule_counts": {
            "spce_water": request["water_count"],
            "sodium": request["sodium_count"],
            "chloride": request["chloride_count"],
        },
        "atom_count": len(records),
    }


def packed_fluid_tsv(
    records: list[dict[str, Any]], frame: dict[str, Any], request: dict[str, Any]
) -> str:
    lengths = frame["lengths_angstrom"]
    shift = frame["normal_shift_angstrom"]
    offset = frame["ledger"]["model"]["atom_count"]
    fluid = records[offset:]
    lines = ["component\tmolecule_index\tsite\telement\tname\tformal_charge_num\tformal_charge_den\tfx\tfy\tfz"]
    cursor = 0
    for molecule in range(request["water_count"]):
        for site, name in (("O", "OW"), ("H1", "HW1"), ("H2", "HW2")):
            record = fluid[cursor]
            cursor += 1
            x, y, shifted_z = record["local_xyz"]
            z = (shifted_z - shift) % lengths[2]
            lines.append(
                f"water\t{molecule}\t{site}\t{record['element']}\t{name}\t0\t1\t"
                f"{(x / lengths[0]) % 1.0:.15g}\t{(y / lengths[1]) % 1.0:.15g}\t{z / lengths[2]:.15g}"
            )
    for component, count, element, charge in (
        ("sodium", request["sodium_count"], "Na", 1),
        ("chloride", request["chloride_count"], "Cl", -1),
    ):
        for molecule in range(count):
            record = fluid[cursor]
            cursor += 1
            x, y, shifted_z = record["local_xyz"]
            z = (shifted_z - shift) % lengths[2]
            lines.append(
                f"{component}\t{molecule}\tION\t{element}\t{element}\t{charge}\t1\t"
                f"{(x / lengths[0]) % 1.0:.15g}\t{(y / lengths[1]) % 1.0:.15g}\t{z / lengths[2]:.15g}"
            )
    if cursor != len(fluid):
        raise RuntimeError("Packed fluid ledger length does not match the requested composition")
    return "\n".join(lines) + "\n"


def build_packed_fluid_import_script() -> str:
    return r'''use strict;
use warnings;
use MaterialsScript qw(:all);
my $doc = Documents->Import("{{input.structure}}");
open(my $ledger, '<', "{{input.fluid_ledger}}") or die $!;
my $header = <$ledger>;
my %water_oxygen;
while (my $line = <$ledger>) {
    chomp $line;
    next unless length($line);
    my ($component, $molecule, $site, $element, $name, $charge_num, $charge_den, $fx, $fy, $fz) = split(/\t/, $line);
    my $atom = $doc->CreateAtom($element,
        $doc->FromFractionalPosition(Point(X => $fx, Y => $fy, Z => $fz)));
    $atom->Name = $name;
    my $charge = $atom->FormalCharge;
    $charge->Numerator = $charge_num;
    $charge->Denominator = $charge_den;
    $atom->FormalCharge = $charge;
    if ($component eq 'water') {
        if ($site eq 'O') {
            $water_oxygen{$molecule} = $atom;
        } else {
            die "Water hydrogen precedes oxygen for molecule $molecule" unless exists $water_oxygen{$molecule};
            $doc->CreateBond($water_oxygen{$molecule}, $atom, "Single");
        }
    }
}
close($ledger);
$doc->Export("{{output.structure}}");
$doc->Close;
'''


def validate_packed_xsd(
    input_model: dict[str, Any], output_path: Path, request: dict[str, Any]
) -> dict[str, Any]:
    output = inspect_xsd_geometry(output_path)
    expected_elements = Counter(input_model["elements"])
    expected_elements.update(
        {
            "O": request["water_count"],
            "H": 2 * request["water_count"],
            "Na": request["sodium_count"],
            "Cl": request["chloride_count"],
        }
    )
    errors: list[str] = []
    if output["periodic_dimension"] != 3:
        errors.append("packed output is not three-dimensionally periodic")
    if output["atom_count"] != request["expected_total_atoms"]:
        errors.append("packed output atom count does not match the exact request")
    if output["bond_count"] != request["expected_total_bonds"]:
        errors.append("packed output bond count does not preserve the framework plus two bonds per water")
    if output["elements"] != dict(sorted(expected_elements.items())):
        errors.append("packed output element inventory does not match the exact request")
    if not math.isclose(
        output["formal_charge_e"], request["expected_final_formal_charge_e"], abs_tol=1.0e-10
    ):
        errors.append("packed output formal charge does not match the required value")
    for axis in ("AVector", "BVector", "CVector"):
        before = input_model["cell_vectors"][axis]
        after = output["cell_vectors"][axis]
        if any(not math.isclose(a, b, abs_tol=1.0e-8) for a, b in zip(before, after)):
            errors.append(f"{axis} changed during packing")
    if errors:
        raise RuntimeError("Packed XSD post-validation failed: " + "; ".join(errors))
    return {
        "status": "periodic_aqueous_nacl_packing_pass",
        "production_released": False,
        "expected": {
            "atom_count": request["expected_total_atoms"],
            "bond_count": request["expected_total_bonds"],
            "elements": dict(sorted(expected_elements.items())),
            "formal_charge_e": request["expected_final_formal_charge_e"],
        },
        "output": output,
    }
