from __future__ import annotations

from collections import Counter, defaultdict
import json
import math
from pathlib import Path
import re
from typing import Any

from .geology_modeling import inspect_explicit_bond_pairs, inspect_p1_atom_ledger, sha256_file
from .periodic_packing import periodic_orthorhombic_frame


TYPE_ORDER = ("Sih", "Sis", "Sib", "Oh", "Os", "Ob", "Hh", "Ow", "Hw", "Na", "Cl")
EXPECTED_TYPE_COUNTS = {
    "Sih": 256,
    "Sis": 128,
    "Sib": 576,
    "Oh": 256,
    "Os": 512,
    "Ob": 1280,
    "Hh": 256,
    "Ow": 5340,
    "Hw": 10680,
    "Na": 40,
    "Cl": 40,
}
TYPE_CHARGES = {
    "Sih": 2.1,
    "Sis": 2.1,
    "Sib": 2.1,
    "Oh": -0.95,
    "Os": -1.05,
    "Ob": -1.05,
    "Hh": 0.425,
    "Ow": -0.8476,
    "Hw": 0.4238,
    "Na": 1.0,
    "Cl": -1.0,
}
TYPE_MASSES = {
    "Sih": 28.0855,
    "Sis": 28.0855,
    "Sib": 28.0855,
    "Oh": 15.9994,
    "Os": 15.9994,
    "Ob": 15.9994,
    "Hh": 1.00794,
    "Ow": 15.9994,
    "Hw": 1.00794,
    "Na": 22.98976928,
    "Cl": 35.453,
}


def _cluster_indices(atoms: list[dict[str, Any]], z_by_index: dict[int, float], tolerance: float = 0.08) -> list[set[int]]:
    clusters: list[set[int]] = []
    centers: list[float] = []
    for atom in sorted(atoms, key=lambda item: z_by_index[item["atom_index"]]):
        z = z_by_index[atom["atom_index"]]
        if not centers or abs(z - centers[-1]) > tolerance:
            centers.append(z)
            clusters.append(set())
        clusters[-1].add(atom["atom_index"])
    return clusters


def classify_neutral_quartz_spce_nacl(path: Path, framework_atom_count: int = 3264) -> dict[str, Any]:
    frame = periodic_orthorhombic_frame(path)
    ledger = frame["ledger"]
    if ledger["model"]["atom_count"] != 19364:
        raise ValueError("Neutral G06 packed candidate must contain exactly 19364 atoms")
    if framework_atom_count != 3264:
        raise ValueError("Neutral G06 framework atom count must be exactly 3264")
    bonds = inspect_explicit_bond_pairs(path)
    adjacency: dict[int, set[int]] = defaultdict(set)
    for left, right in bonds:
        adjacency[left].add(right)
        adjacency[right].add(left)

    framework = ledger["atoms"][:framework_atom_count]
    fluid = ledger["atoms"][framework_atom_count:]
    if Counter(atom["element"] for atom in framework) != Counter({"Si": 960, "O": 2048, "H": 256}):
        raise ValueError("Packed candidate does not preserve the exact G06 quartz framework inventory")
    z_by_index = {atom["atom_index"]: frame["local_atoms"][atom["atom_index"]]["local_xyz"][2] for atom in framework}

    oh = {
        atom["atom_index"]
        for atom in framework
        if atom["element"] == "O"
        and any(ledger["by_index"][neighbor]["element"] == "H" for neighbor in adjacency[atom["atom_index"]])
    }
    hh = {
        atom["atom_index"]
        for atom in framework
        if atom["element"] == "H"
        and any(neighbor in oh for neighbor in adjacency[atom["atom_index"]])
    }
    sih = {
        neighbor
        for oxygen in oh
        for neighbor in adjacency[oxygen]
        if ledger["by_index"][neighbor]["element"] == "Si"
    }

    si_atoms = [atom for atom in framework if atom["element"] == "Si"]
    o_atoms = [atom for atom in framework if atom["element"] == "O"]
    si_clusters = _cluster_indices(si_atoms, z_by_index)
    o_clusters = _cluster_indices(o_atoms, z_by_index)
    if len(si_clusters) != 15 or any(len(cluster) != 64 for cluster in si_clusters):
        raise ValueError("G06 quartz must contain fifteen explicit 64-Si normal layers")
    if len(o_clusters) != 32 or any(len(cluster) != 64 for cluster in o_clusters):
        raise ValueError("G06 quartz must contain thirty-two explicit 64-O normal layers")
    surface_si = set().union(*si_clusters[:3], *si_clusters[-3:])
    surface_o = set().union(*o_clusters[:6], *o_clusters[-6:])
    if not sih <= surface_si or not oh <= surface_o:
        raise ValueError("Hydroxyl topology is inconsistent with the outer G06 surface layers")
    sis = surface_si - sih
    sib = {atom["atom_index"] for atom in si_atoms} - surface_si
    os_indices = surface_o - oh
    ob = {atom["atom_index"] for atom in o_atoms} - surface_o

    type_by_index: dict[int, str] = {}
    for name, indices in (("Sih", sih), ("Sis", sis), ("Sib", sib), ("Oh", oh), ("Os", os_indices), ("Ob", ob), ("Hh", hh)):
        for atom_index in indices:
            type_by_index[atom_index] = name

    for atom in fluid:
        name = atom["name"]
        element = atom["element"]
        if name == "OW" and element == "O":
            atom_type = "Ow"
        elif name in {"HW1", "HW2"} and element == "H":
            atom_type = "Hw"
        elif name == "Na" and element == "Na":
            atom_type = "Na"
        elif name == "Cl" and element == "Cl":
            atom_type = "Cl"
        else:
            raise ValueError(f"Unexpected packed-fluid atom identity: {name}/{element}")
        type_by_index[atom["atom_index"]] = atom_type

    observed_counts = Counter(type_by_index.values())
    if observed_counts != Counter(EXPECTED_TYPE_COUNTS):
        raise ValueError(f"G06 SI Table S1 type counts do not match: {dict(sorted(observed_counts.items()))}")
    if len(type_by_index) != ledger["model"]["atom_count"]:
        raise ValueError("G06 atom typing did not cover every atom")

    structural_bonds: list[tuple[int, int]] = []
    water_bonds: list[tuple[int, int]] = []
    for left, right in bonds:
        pair_types = {type_by_index.get(left), type_by_index.get(right)}
        if pair_types == {"Oh", "Hh"}:
            structural_bonds.append((left, right))
        elif pair_types == {"Ow", "Hw"}:
            water_bonds.append((left, right))
    if len(structural_bonds) != 256 or len(water_bonds) != 10680:
        raise ValueError("G06 retained O-H topology does not match the exact structural/water bond counts")

    structural_angles: list[tuple[int, int, int]] = []
    for oxygen in sorted(oh):
        silicon = [index for index in adjacency[oxygen] if type_by_index.get(index) == "Sih"]
        hydrogen = [index for index in adjacency[oxygen] if type_by_index.get(index) == "Hh"]
        if len(silicon) != 1 or len(hydrogen) != 1:
            raise ValueError("Each G06 Oh atom must define exactly one Sih-Oh-Hh angle")
        structural_angles.append((silicon[0], oxygen, hydrogen[0]))

    water_angles: list[tuple[int, int, int]] = []
    water_molecules: list[tuple[int, int, int]] = []
    for oxygen in sorted(index for index, atom_type in type_by_index.items() if atom_type == "Ow"):
        hydrogen = sorted(index for index in adjacency[oxygen] if type_by_index.get(index) == "Hw")
        if len(hydrogen) != 2:
            raise ValueError("Each SPC/E oxygen must be bonded to exactly two water hydrogens")
        water_angles.append((hydrogen[0], oxygen, hydrogen[1]))
        water_molecules.append((oxygen, hydrogen[0], hydrogen[1]))
    if len(structural_angles) != 256 or len(water_angles) != 5340:
        raise ValueError("G06 angle topology does not match the exact request")

    molecule_by_index = {index: 1 for index in range(framework_atom_count)}
    molecule = 1
    for triplet in water_molecules:
        molecule += 1
        for atom_index in triplet:
            molecule_by_index[atom_index] = molecule
    for atom_index in sorted(
        index for index, atom_type in type_by_index.items() if atom_type in {"Na", "Cl"}
    ):
        molecule += 1
        molecule_by_index[atom_index] = molecule
    if len(molecule_by_index) != ledger["model"]["atom_count"]:
        raise ValueError("G06 molecule assignment did not cover every atom")

    net_charge = math.fsum(TYPE_CHARGES[type_by_index[index]] for index in range(len(type_by_index)))
    if not math.isclose(net_charge, 0.0, abs_tol=1.0e-9):
        raise ValueError(f"G06 ClayFF/SPC/E/Joung type assignment is not neutral: {net_charge}")
    return {
        "frame": frame,
        "ledger": ledger,
        "type_by_index": type_by_index,
        "molecule_by_index": molecule_by_index,
        "structural_bonds": sorted(structural_bonds),
        "water_bonds": sorted(water_bonds),
        "structural_angles": structural_angles,
        "water_angles": water_angles,
        "type_counts": dict(sorted(observed_counts.items())),
        "net_charge_e": net_charge,
    }


def validate_forcefield_sources(clay_nonbonded: Path, clay_bonded: Path, joung_spce: Path) -> dict[str, Any]:
    nonbonded = clay_nonbonded.read_text(encoding="utf-8")
    bonded = clay_bonded.read_text(encoding="utf-8")
    joung = joung_spce.read_text(encoding="utf-8")
    required_nonbonded = {
        "st": (2.1, 0.33020, 0.000007701),
        "ob": (-1.05, 0.31655, 0.6502),
        "oh": (-0.95, 0.31655, 0.6502),
        "ho": (0.425, 0.0, 0.0),
    }
    parsed: dict[str, tuple[float, float, float]] = {}
    for line in nonbonded.splitlines():
        fields = line.split(";", 1)[0].split()
        if len(fields) >= 7 and fields[0] in required_nonbonded:
            parsed[fields[0]] = (float(fields[3]), float(fields[5]), float(fields[6]))
    for name, expected in required_nonbonded.items():
        if name not in parsed or any(not math.isclose(a, b, rel_tol=0.0, abs_tol=1.0e-9) for a, b in zip(parsed[name], expected)):
            raise ValueError(f"ClayFF source does not contain the expected {name} parameters")
    if not re.search(r"^oh\s+ho\s+1\s+0\.1\s+463532\.8\s*$", bonded, re.MULTILINE):
        raise ValueError("ClayFF source does not contain the expected structural O-H bond")
    ion_rows: dict[str, tuple[float, float]] = {}
    for line in joung.splitlines():
        fields = line.split()
        if len(fields) == 3 and fields[0] in {"Na+", "Cl-"}:
            try:
                ion_rows[fields[0]] = (float(fields[1]), float(fields[2]))
            except ValueError:
                pass
    expected_ions = {"Na+": (1.212, 0.3526418), "Cl-": (2.711, 0.0127850)}
    if ion_rows != expected_ions:
        raise ValueError(f"Joung-Cheatham SPC/E ion source differs: {ion_rows}")
    return {
        "clay_nonbonded_sha256": sha256_file(clay_nonbonded),
        "clay_bonded_sha256": sha256_file(clay_bonded),
        "joung_cheatham_spce_sha256": sha256_file(joung_spce),
        "clay_source_parameters": {name: list(values) for name, values in required_nonbonded.items()},
        "joung_cheatham_amber_rmin_over_2_epsilon": {name: list(values) for name, values in expected_ions.items()},
    }


def forcefield_profile(source_evidence: dict[str, Any]) -> dict[str, Any]:
    sigma_na = 2.0 * 1.212 / (2.0 ** (1.0 / 6.0))
    sigma_cl = 2.0 * 2.711 / (2.0 ** (1.0 / 6.0))
    pair: dict[str, dict[str, float]] = {}
    for name in ("Sih", "Sis", "Sib"):
        pair[name] = {"epsilon_kcal_mol": 0.000007701 / 4.184, "sigma_angstrom": 3.3020}
    for name in ("Oh", "Os", "Ob"):
        pair[name] = {"epsilon_kcal_mol": 0.6502 / 4.184, "sigma_angstrom": 3.1655}
    pair["Hh"] = {"epsilon_kcal_mol": 0.0, "sigma_angstrom": 0.0}
    pair["Ow"] = {"epsilon_kcal_mol": 0.1553, "sigma_angstrom": 3.1660}
    pair["Hw"] = {"epsilon_kcal_mol": 0.0, "sigma_angstrom": 0.0}
    pair["Na"] = {"epsilon_kcal_mol": 0.3526418, "sigma_angstrom": sigma_na}
    pair["Cl"] = {"epsilon_kcal_mol": 0.0127850, "sigma_angstrom": sigma_cl}
    return {
        "schema_version": 1,
        "profile_id": "G06-CLAYFF-SPCE-JC2008-NEUTRAL-V1",
        "units": "LAMMPS real",
        "mixing": "Lorentz-Berthelot: arithmetic sigma, geometric epsilon",
        "charges_e": TYPE_CHARGES,
        "masses_g_mol": TYPE_MASSES,
        "pair_coefficients": pair,
        "bond_coefficients": {
            "structural_Oh-Hh": {"lammps_k_kcal_mol_angstrom2": 463532.8 / (2.0 * 4.184 * 100.0), "r0_angstrom": 1.0},
            "SPCE_Ow-Hw_SHAKE_reference": {"lammps_k_kcal_mol_angstrom2": 345000.0 / (2.0 * 4.184 * 100.0), "r0_angstrom": 1.0},
        },
        "angle_coefficients": {
            "Sih-Oh-Hh": {"lammps_k_kcal_mol_rad2": 251.2 / (2.0 * 4.184), "theta0_degrees": 109.47},
            "SPCE_Hw-Ow-Hw_SHAKE_reference": {"lammps_k_kcal_mol_rad2": 383.0 / (2.0 * 4.184), "theta0_degrees": 109.47},
        },
        "source_evidence": source_evidence,
    }


def render_lammps_data(classification: dict[str, Any], profile: dict[str, Any]) -> str:
    type_ids = {name: index for index, name in enumerate(TYPE_ORDER, 1)}
    ledger = classification["ledger"]
    frame = classification["frame"]
    lengths = frame["lengths_angstrom"]
    bonds = [(1, *pair) for pair in classification["structural_bonds"]]
    bonds += [(2, *pair) for pair in classification["water_bonds"]]
    angles = [(1, *triple) for triple in classification["structural_angles"]]
    angles += [(2, *triple) for triple in classification["water_angles"]]
    lines = [
        "G06 quartz (101), neutral 0.4 M NaCl, ClayFF/SPC/E/Joung-Cheatham candidate",
        "",
        f"{ledger['model']['atom_count']} atoms",
        f"{len(bonds)} bonds",
        f"{len(angles)} angles",
        "0 dihedrals",
        "0 impropers",
        "",
        f"{len(TYPE_ORDER)} atom types",
        "2 bond types",
        "2 angle types",
        "0 dihedral types",
        "0 improper types",
        "",
        f"0.0 {lengths[0]:.12f} xlo xhi",
        f"0.0 {lengths[1]:.12f} ylo yhi",
        f"0.0 {lengths[2]:.12f} zlo zhi",
        "",
        "Masses",
        "",
    ]
    for name in TYPE_ORDER:
        lines.append(f"{type_ids[name]} {TYPE_MASSES[name]:.10f} # {name}")
    lines.extend(["", "Pair Coeffs", ""])
    for name in TYPE_ORDER:
        record = profile["pair_coefficients"][name]
        lines.append(f"{type_ids[name]} {record['epsilon_kcal_mol']:.12g} {record['sigma_angstrom']:.12g} # {name}")
    bond_profile = profile["bond_coefficients"]
    angle_profile = profile["angle_coefficients"]
    lines.extend(
        [
            "",
            "Bond Coeffs",
            "",
            f"1 {bond_profile['structural_Oh-Hh']['lammps_k_kcal_mol_angstrom2']:.12g} 1.0 # Oh-Hh",
            f"2 {bond_profile['SPCE_Ow-Hw_SHAKE_reference']['lammps_k_kcal_mol_angstrom2']:.12g} 1.0 # Ow-Hw SHAKE",
            "",
            "Angle Coeffs",
            "",
            f"1 {angle_profile['Sih-Oh-Hh']['lammps_k_kcal_mol_rad2']:.12g} 109.47 # Sih-Oh-Hh",
            f"2 {angle_profile['SPCE_Hw-Ow-Hw_SHAKE_reference']['lammps_k_kcal_mol_rad2']:.12g} 109.47 # Hw-Ow-Hw SHAKE",
            "",
            "Atoms # full",
            "",
        ]
    )
    for atom in ledger["atoms"]:
        index = atom["atom_index"]
        atom_type = classification["type_by_index"][index]
        x, y, z = frame["local_atoms"][index]["local_xyz"]
        lines.append(
            f"{index + 1} {classification['molecule_by_index'][index]} {type_ids[atom_type]} "
            f"{TYPE_CHARGES[atom_type]:.8f} {x:.12f} {y:.12f} {z:.12f} # {atom_type}"
        )
    lines.extend(["", "Bonds", ""])
    for bond_id, (bond_type, left, right) in enumerate(bonds, 1):
        lines.append(f"{bond_id} {bond_type} {left + 1} {right + 1}")
    lines.extend(["", "Angles", ""])
    for angle_id, (angle_type, left, center, right) in enumerate(angles, 1):
        lines.append(f"{angle_id} {angle_type} {left + 1} {center + 1} {right + 1}")
    return "\n".join(lines) + "\n"


def _common_input(data_filename: str) -> str:
    return f"""units real
atom_style full
boundary p p p
pair_style lj/cut/coul/long 10.0
bond_style harmonic
angle_style harmonic
special_bonds lj/coul 0.0 0.0 1.0
read_data {data_filename}

pair_modify mix arithmetic tail yes
kspace_style pppm 1.0e-5
neighbor 2.0 bin
neigh_modify every 1 delay 0 check yes

group substrate type 1:7
group water type 8 9
group ions type 10 11
group fluid union water ions
velocity substrate set 0.0 0.0 0.0
fix substrate_hold substrate setforce 0.0 0.0 0.0
fix water_constraints water shake 0.0001 20 0 b 2 a 2
compute fluid_temperature fluid temp
"""


def render_gate_input(data_filename: str) -> str:
    return _common_input(data_filename) + """
thermo 10
thermo_style custom step atoms temp press pe ke etotal ebond eangle evdwl ecoul elong vol lx ly lz
thermo_modify temp fluid_temperature flush yes lost error
dump gate all custom 100 gate.lammpstrj id mol type q x y z ix iy iz
dump_modify gate sort id

print "G06_PHASE_RUN0_BEGIN"
run 0 post yes
print "G06_PHASE_RUN0_END"

print "G06_PHASE_MIN_BEGIN"
unfix water_constraints
min_style fire
min_modify dmax 0.05
minimize 0.0 1.0e-1 10000 100000
fix water_constraints water shake 0.0001 20 0 b 2 a 2
print "G06_PHASE_MIN_END"

undump gate
reset_timestep 0
timestep 1.0
velocity fluid create 298.0 2026071602 mom yes rot yes dist gaussian
fix gate_nvt fluid nvt temp 298.0 298.0 100.0
fix_modify gate_nvt temp fluid_temperature
dump gate_md all custom 100 gate_md.lammpstrj id mol type q x y z ix iy iz
dump_modify gate_md sort id
print "G06_PHASE_SHORT_MD_BEGIN"
run 1000 post yes
print "G06_PHASE_SHORT_MD_END"
unfix gate_nvt
write_data g06_gate_final.data
write_restart g06_gate_final.restart
"""


def render_production_input(data_filename: str) -> str:
    return _common_input(data_filename) + """
thermo 1000
thermo_style custom step atoms temp press pxx pyy pzz pe ke etotal evdwl ecoul elong density vol lx ly lz
thermo_modify temp fluid_temperature flush yes lost error
timestep 1.0
velocity fluid create 298.0 2026071603 mom yes rot yes dist gaussian
restart 100000 g06_restart_a g06_restart_b

fix stage_npt fluid npt temp 298.0 298.0 100.0 z 1.0 1.0 1000.0 couple none dilate all
fix_modify stage_npt temp fluid_temperature
run 500000
unfix stage_npt
write_data g06_after_500ps_npt.data

fix stage_nvt fluid nvt temp 298.0 298.0 100.0
fix_modify stage_nvt temp fluid_temperature
run 250000
unfix stage_nvt
write_data g06_after_250ps_nvt.data

reset_timestep 0
dump production all dcd 5000 g06_production_17p5ns.dcd
dump_modify production unwrap yes
fix production_nvt fluid nvt temp 298.0 298.0 100.0
fix_modify production_nvt temp fluid_temperature
run 17500000
unfix production_nvt
write_data g06_production_final.data
write_restart g06_production_final.restart
"""


def protocol_contract(
    *, input_sha256: str, packing_receipt_sha256: str, methods_sha256: str, si_sha256: str,
    forcefield_profile_sha256: str, data_sha256: str, gate_input_sha256: str,
    production_input_sha256: str, observed_cell_lengths: list[float], type_counts: dict[str, int],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "contract_id": "G06-JPCC-7B08214-NEUTRAL-NACL-RECONSTRUCTION-V1",
        "status": "deterministic_protocol_reconstruction_candidate",
        "production_released": False,
        "target": {"doi": "10.1021/acs.jpcc.7b08214", "surface_charge_C_m2": 0.0, "electrolyte": "0.4 M NaCl"},
        "hash_bindings": {
            "packed_xsd_sha256": input_sha256,
            "packing_receipt_sha256": packing_receipt_sha256,
            "authenticated_methods_sha256": methods_sha256,
            "supporting_information_sha256": si_sha256,
            "forcefield_profile_sha256": forcefield_profile_sha256,
            "lammps_data_sha256": data_sha256,
            "gate_input_sha256": gate_input_sha256,
            "production_input_sha256": production_input_sha256,
        },
        "composition": {"type_counts": type_counts, "total_atoms": 19364, "total_charge_e": 0.0},
        "cell": {
            "paper_initial_angstrom": [55.0, 39.82, 90.21],
            "reconstruction_initial_angstrom": observed_cell_lengths,
            "paper_final_after_npt_angstrom": [55.0, 39.82, 90.82],
            "source_model_difference": "The MS 23.1 built-in quartz parent gives B=39.27997 A; it is not silently rescaled to 39.82 A.",
        },
        "forcefield": {
            "framework": "neutral modified ClayFF charges from Table S1; standard ClayFF LJ and structural OH terms",
            "water": "SPC/E, rigid with SHAKE",
            "ions": "Joung-Cheatham 2008 SPC/E Na+/Cl-",
            "electrostatics": "PPPM 1e-5",
            "mixing": "Lorentz-Berthelot",
        },
        "dynamics": {
            "timestep_fs": 1.0,
            "temperature_K": 298.0,
            "npt": {"duration_ps": 500.0, "pressure_atm": 1.0, "controlled_axis": "z"},
            "nvt_equilibration_ps": 250.0,
            "nvt_production_ns": 17.5,
        },
        "qualification_boundaries": {
            "author_coordinate_identity_claimed": False,
            "deterministic_reconstruction_from_ms_builtin_parent": True,
            "all_framework_atoms_fixed_in_lammps_candidate": True,
            "reason": "The authenticated main-method text does not expose an author coordinate file or a complete fixed-layer atom ledger.",
            "long_production_executed": False,
        },
    }
