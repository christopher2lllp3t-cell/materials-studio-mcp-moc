from __future__ import annotations

import re
from typing import Any


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
AXES = ("x", "y", "z")
REQUIRED_STREAMS = ("packing", "velocity", "thermostat", "sorption")


def _object(parent: dict[str, Any], key: str, errors: list[str]) -> dict[str, Any]:
    value = parent.get(key)
    if not isinstance(value, dict):
        errors.append(f"science_contract.{key} must be an object")
        return {}
    return value


def validate_science_contract(manifest: dict[str, Any]) -> dict[str, Any]:
    """Validate the immutable scientific semantics needed before production.

    This check deliberately validates meaning rather than merely file shape. It
    is dependency-free so every MCP entry point can fail closed before spawning
    Materials Studio, msi2lmp, LAMMPS, or VMD.
    """
    errors: list[str] = []
    warnings: list[str] = []
    contract = manifest.get("science_contract")
    if not isinstance(contract, dict):
        return {"status": "fail", "production_allowed": False,
                "errors": ["science_contract is required"], "warnings": []}

    if contract.get("schema_version") != 1:
        errors.append("science_contract.schema_version must equal 1")

    units = _object(contract, "units", errors)
    allowed_unit_profiles = {
        "ms_compass_to_lammps_real_v1",
        "ms_pcff_to_lammps_real_v1",
    }
    if units.get("profile") not in allowed_unit_profiles:
        errors.append(
            "science_contract.units.profile must be a reviewed MS-to-LAMMPS real-units profile"
        )
    required_units = {
        "lammps_style": "real",
        "length": "angstrom", "time": "femtosecond", "energy": "kcal/mol",
        "charge": "elementary_charge", "mass": "g/mol", "pressure": "atm",
    }
    for key, expected in required_units.items():
        if units.get(key) != expected:
            errors.append(f"science_contract.units.{key} must equal {expected!r}")

    coordinates = _object(contract, "coordinates", errors)
    if coordinates.get("representation") not in {"fractional", "cartesian"}:
        errors.append("science_contract.coordinates.representation must be fractional or cartesian")
    if coordinates.get("length_unit") != "angstrom":
        errors.append("science_contract.coordinates.length_unit must equal 'angstrom'")
    if coordinates.get("cell_matrix_convention") != "row_vectors_a_b_c":
        errors.append("science_contract.coordinates.cell_matrix_convention must equal 'row_vectors_a_b_c'")

    boundary = _object(contract, "boundary", errors)
    periodic_axes = boundary.get("periodic_axes")
    styles = boundary.get("lammps_boundary")
    if not isinstance(periodic_axes, list) or len(set(periodic_axes)) != len(periodic_axes) or any(a not in AXES for a in periodic_axes):
        errors.append("science_contract.boundary.periodic_axes must contain unique x/y/z values")
        periodic_axes = []
    if not isinstance(styles, list) or len(styles) != 3 or any(s not in {"p", "f", "s", "m"} for s in styles):
        errors.append("science_contract.boundary.lammps_boundary must be three p/f/s/m values")
    else:
        encoded = [axis for axis, style in zip(AXES, styles) if style == "p"]
        if set(encoded) != set(periodic_axes):
            errors.append("periodic_axes must exactly match axes marked p in lammps_boundary")

    lammps = _object(contract, "lammps", errors)
    if lammps.get("atom_style") != "full":
        errors.append("science_contract.lammps.atom_style must equal 'full' in v1")
    if lammps.get("triclinic_tilt_handling") != "preserve":
        errors.append("science_contract.lammps.triclinic_tilt_handling must equal 'preserve'")

    charge = _object(contract, "charge", errors)
    if charge.get("formal_charge_semantics") != "oxidation_or_connectivity_only":
        errors.append("formal charge semantics must be oxidation_or_connectivity_only")
    if charge.get("partial_charge_semantics") != "forcefield_nonbonded_charge":
        errors.append("partial charge semantics must be forcefield_nonbonded_charge")
    if not isinstance(charge.get("partial_charge_source"), str) or not charge.get("partial_charge_source", "").strip():
        errors.append("science_contract.charge.partial_charge_source is required")
    expected_charge = charge.get("expected_net_partial_charge_e")
    if not isinstance(expected_charge, (int, float)) or isinstance(expected_charge, bool):
        errors.append("science_contract.charge.expected_net_partial_charge_e must be numeric")
    if charge.get("audit_by_component") is not True:
        errors.append("science_contract.charge.audit_by_component must be true")

    forcefield = _object(contract, "forcefield", errors)
    for key in ("profile_id", "profile_version"):
        if not isinstance(forcefield.get(key), str) or not forcefield.get(key, "").strip():
            errors.append(f"science_contract.forcefield.{key} is required")
    if not SHA256_RE.fullmatch(str(forcefield.get("profile_sha256", ""))):
        errors.append("science_contract.forcefield.profile_sha256 must be a lowercase SHA-256")
    hashes = forcefield.get("parameter_file_sha256")
    if not isinstance(hashes, list) or not hashes or any(not SHA256_RE.fullmatch(str(item)) for item in hashes):
        errors.append("science_contract.forcefield.parameter_file_sha256 must contain valid SHA-256 values")
    if forcefield.get("compatibility_reviewed") is not True:
        errors.append("science_contract.forcefield.compatibility_reviewed must be true")
    for key in ("mixing_rule", "special_bonds", "long_range_electrostatics"):
        if not isinstance(forcefield.get(key), str) or not forcefield.get(key, "").strip():
            errors.append(f"science_contract.forcefield.{key} is required")

    seeds = _object(contract, "seed_ledger", errors)
    seed = seeds.get("master_seed")
    if not isinstance(seed, int) or isinstance(seed, bool) or not 1 <= seed <= 2147483647:
        errors.append("science_contract.seed_ledger.master_seed must be an int in 1..2147483647")
    if seeds.get("derivation") != "sha256_run_uuid_stage_replica_to_int31_v1":
        errors.append("science_contract.seed_ledger.derivation must use the v1 deterministic rule")
    if not isinstance(seeds.get("replica_index"), int) or seeds.get("replica_index", -1) < 0:
        errors.append("science_contract.seed_ledger.replica_index must be a non-negative integer")
    streams = seeds.get("streams")
    if not isinstance(streams, dict) or set(streams) != set(REQUIRED_STREAMS):
        errors.append("seed_ledger.streams must contain exactly packing/velocity/thermostat/sorption")
    elif any(not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 2147483647 for value in streams.values()):
        errors.append("all derived seed streams must be int values in 1..2147483647")

    trajectory = _object(contract, "trajectory", errors)
    if trajectory.get("wrapped_fields") not in {"x_y_z", "xs_ys_zs"}:
        errors.append("trajectory.wrapped_fields must explicitly select x_y_z or xs_ys_zs")
    if trajectory.get("unwrapped_fields") not in {"xu_yu_zu", "x_y_z_plus_image_flags"}:
        errors.append("trajectory.unwrapped_fields must define an unwrapped representation")
    if trajectory.get("msd_uses_unwrapped") is not True:
        errors.append("trajectory.msd_uses_unwrapped must be true")
    if trajectory.get("density_uses_wrapped") is not True:
        errors.append("trajectory.density_uses_wrapped must be true")
    if trajectory.get("timestep_unit") != "femtosecond":
        errors.append("trajectory.timestep_unit must equal 'femtosecond'")

    geology = manifest.get("geology_model", {})
    if not isinstance(geology, dict):
        errors.append("geology_model must be an object")
        geology = {}
    fidelity = geology.get("fidelity")
    if fidelity not in {"reviewed_scientific_model", "surrogate_demo"}:
        errors.append("geology_model.fidelity must be reviewed_scientific_model or surrogate_demo")
    surrogate = fidelity == "surrogate_demo" or geology.get("clay_is_surrogate") is True
    if surrogate:
        warnings.append("SURROGATE_MODEL: allowed for pipeline tests only")

    production_allowed = not errors and not surrogate
    if manifest.get("project", {}).get("status") == "production" and surrogate:
        errors.append("SURROGATE_MODEL_BLOCKS_PRODUCTION")
        production_allowed = False
    return {"validator": "materials_studio_mcp.science_contract:v1",
            "status": "pass" if not errors else "fail",
            "production_allowed": production_allowed, "surrogate_model": surrogate,
            "errors": errors, "warnings": warnings}
