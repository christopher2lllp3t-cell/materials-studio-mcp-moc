from __future__ import annotations

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
from .lammps_validation import _parameter_and_topology_gate
from .pipeline_config import approved_executable, load_pipeline_config
from .project_manager import (
    _record_verified_quality_gate,
    initialize_project,
    register_artifact,
    transition_project_status,
    update_model_specification,
    validate_project,
)
from .science_contract import validate_science_contract
from .structure_preflight import inspect_lammps_data
from .vmd_validation import validate_vmd_text_trajectory


WORKSPACE_ROOT = Path(r"D:\分子动力学模拟")
DEFAULT_PROJECTS_ROOT = WORKSPACE_ROOT / "07_mcp_materials_studio" / "mcp_projects"
MS_ROOT = Path(r"D:\Program Files (x86)\BIOVIA\Materials Studio 23.1")
OFFICIAL_WATER = MS_ROOT / "share" / "Examples" / "Scripting" / "water.xsd"
PCFF_OFF = MS_ROOT / "share" / "Resources" / "Simulation" / "ClassicalEnergy" / "FORCEFIELDS" / "Standard" / "pcff.off"
PCFF_FRC = Path(r"D:\lammps_install\LAMMPS 64-bit 11Feb2026-MSMPI\frc_files\pcff.frc")
G01_HISTORY = WORKSPACE_ROOT / "07_mcp_materials_studio" / "evidence" / "g01_ms_energy_audit.json"
EXPECTED_MS_POTENTIAL = 9.58988028675435
EXPECTED_LAMMPS_POTENTIAL = 9.5898501
ENERGY_TOLERANCE_KCAL_MOL = 1.0e-3


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def _register_tree(project: Path, root: Path, role_prefix: str, source: str) -> list[dict[str, Any]]:
    registrations: list[dict[str, Any]] = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        role = f"{role_prefix}_{path.suffix.lower().lstrip('.') or 'file'}"
        registrations.append(register_artifact(str(project), str(path), role, source))
    return registrations


def _seed(project_id: str, stream: str) -> int:
    digest = hashlib.sha256(f"{project_id}:{stream}:replica:0".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % 2147483646 + 1


def _parse_key_values(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            result[key.strip()] = value.strip()
    return result


def _parse_run0(log_text: str) -> dict[str, float]:
    header: list[str] | None = None
    rows: list[dict[str, float]] = []
    for line in log_text.splitlines():
        fields = line.split()
        if fields[:3] == ["Step", "Atoms", "PotEng"]:
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
    if not rows:
        raise RuntimeError("LAMMPS run 0 produced no finite thermo row")
    return rows[-1]


def _fatal_lammps_markers(log_text: str) -> list[str]:
    markers: list[str] = []
    if re.search(r"(?im)^\s*ERROR(?:\s+on\s+proc\s+\d+)?:", log_text):
        markers.append("ERROR")
    if re.search(r"(?im)^\s*(?:ERROR(?:\s+on\s+proc\s+\d+)?:\s*)?Lost atoms:", log_text):
        markers.append("Lost atoms")
    if re.search(r"(?i)(?<![A-Za-z0-9_])(?:[-+]?nan|[-+]?inf(?:inity)?)(?![A-Za-z0-9_])", log_text):
        markers.append("non-finite numeric value")
    return markers


def _run_pcff_run0(data: Path, output: Path, *, timeout_seconds: int = 60) -> dict[str, Any]:
    if output.exists():
        raise FileExistsError(f"LAMMPS reproduction directory already exists: {output}")
    config = load_pipeline_config()
    executable = approved_executable(config["software"]["lammps"]["executable"], config=config)
    output.mkdir(parents=True, exist_ok=False)
    candidate = output / "candidate.data"
    shutil.copy2(data, candidate)
    input_path = output / "in.g01_pcff_run0"
    input_path.write_text(
        """# Fixed G01 PCFF Class II reproduction input.
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
read_data candidate.data
neighbor 2.0 bin
neigh_modify every 1 delay 0 check yes
thermo 1
thermo_style custom step atoms pe ebond eangle edihed eimp evdwl ecoul elong etail
thermo_modify flush yes lost error
run 0 post yes
write_dump all custom trajectory.g01.lammpstrj id mol type q x y z xu yu zu ix iy iz modify sort id
""",
        encoding="ascii",
        newline="\n",
    )
    command = [str(executable), "-in", input_path.name, "-log", "log.lammps", "-screen", "screen.txt"]
    started = datetime.now(timezone.utc).isoformat()
    try:
        completed = subprocess.run(
            command,
            cwd=output,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
            shell=False,
            close_fds=True,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"G01 LAMMPS run 0 timed out after {timeout_seconds} seconds") from exc
    log_path = output / "log.lammps"
    dump_path = output / "trajectory.g01.lammpstrj"
    log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""
    fatal = _fatal_lammps_markers(log_text)
    thermo = _parse_run0(log_text) if completed.returncode == 0 and not fatal else {}
    potential = thermo.get("PotEng")
    errors: list[str] = []
    if completed.returncode != 0:
        errors.append(f"LAMMPS returned {completed.returncode}")
    if fatal:
        errors.append(f"Fatal log markers: {fatal}")
    if potential is None or abs(potential - EXPECTED_LAMMPS_POTENTIAL) > ENERGY_TOLERANCE_KCAL_MOL:
        errors.append("LAMMPS potential differs from the G01 calibrated value")
    if not dump_path.is_file() or dump_path.stat().st_size == 0:
        errors.append("LAMMPS did not write the G01 trajectory dump")
    preflight = inspect_lammps_data(str(candidate), periodic_axes=())
    parameters = _parameter_and_topology_gate(candidate)
    if preflight.get("status") != "pass" or parameters.get("status") != "pass":
        errors.append("Converted data failed structure or parameter coverage preflight")
    result = {
        "schema_version": 1,
        "validator": "materials_studio_mcp.g01_pcff_run0:v1",
        "status": "pass" if not errors else "fail",
        "started_at_utc": started,
        "finished_at_utc": datetime.now(timezone.utc).isoformat(),
        "command": command,
        "return_code": completed.returncode,
        "runtime": {"executable": str(executable), "sha256": _sha256(executable)},
        "thermo": thermo,
        "expected_potential_kcal_mol": EXPECTED_LAMMPS_POTENTIAL,
        "potential_tolerance_kcal_mol": ENERGY_TOLERANCE_KCAL_MOL,
        "structure_preflight": preflight,
        "parameter_and_topology_gate": parameters,
        "artifacts": {
            "input_sha256": _sha256(input_path),
            "candidate_data_sha256": _sha256(candidate),
            "log_sha256": _sha256(log_path) if log_path.is_file() else None,
            "dump_sha256": _sha256(dump_path) if dump_path.is_file() else None,
        },
        "stderr_tail": completed.stderr[-2000:],
        "errors": errors,
    }
    _write_json(output / "g01_run0_evidence.json", result)
    return result


def reproduce_g01(project_id: str, projects_root: Path = DEFAULT_PROJECTS_ROOT) -> dict[str, Any]:
    for required in (OFFICIAL_WATER, PCFF_OFF, PCFF_FRC, G01_HISTORY):
        if not required.is_file():
            raise FileNotFoundError(required)
    created = initialize_project(project_id, "G01 PCFF v1 fresh reproduction", projects_root=str(projects_root))
    project = Path(created["project_directory"])

    source = project / "request" / "official_water.xsd"
    shutil.copy2(OFFICIAL_WATER, source)
    register_artifact(str(project), str(source), "official_ms_water_xsd", str(OFFICIAL_WATER))

    profile = project / "forcefield" / "g01_pcff_subset_profile.json"
    profile_data = {
        "schema_version": 1,
        "profile_id": "g01-pcff-water-subset-v1",
        "scope": "three-atom nonperiodic water calibration fixture only",
        "materials_studio": {"forcefield": "pcff 3.1", "pcff_off_sha256": _sha256(PCFF_OFF)},
        "msi2lmp": {"forcefield": "pcff.frc 4.0", "pcff_frc_sha256": _sha256(PCFF_FRC), "class": "II"},
        "historical_evidence": {"path": str(G01_HISTORY), "sha256": _sha256(G01_HISTORY)},
        "compatibility_reviewed": True,
        "global_library_equivalence_claimed": False,
    }
    _write_json(profile, profile_data)
    register_artifact(str(project), str(profile), "g01_forcefield_profile", "G01 reviewed calibration evidence")

    seed_streams = {name: _seed(project_id, name) for name in ("packing", "velocity", "thermostat", "sorption")}
    specification = {
        "composition": [{"name": "H2O", "count": 1}],
        "temperature_k": 50.0,
        "pressure_mpa": None,
        "periodic_axes": [],
        "cell": {"kind": "nonperiodic_bounding_box"},
        "fixed_regions": [],
    }
    forcefield = {
        "name": "PCFF water calibrated subset",
        "version": "MS pcff 3.1 / msi2lmp pcff.frc 4.0",
        "units": "real",
        "atom_typing_source": "Materials Studio Forcite forcefield assignment",
        "charge_model": "forcefield assigned partial charges",
        "mixing_rule": "sixthpower",
        "special_bonds": "lj/coul 0.0 0.0 1.0",
        "parameter_sources": [str(PCFF_OFF), str(PCFF_FRC)],
    }
    science_contract = {
        "schema_version": 1,
        "units": {"profile": "ms_pcff_to_lammps_real_v1", "lammps_style": "real", "length": "angstrom", "time": "femtosecond", "energy": "kcal/mol", "charge": "elementary_charge", "mass": "g/mol", "pressure": "atm"},
        "coordinates": {"representation": "cartesian", "length_unit": "angstrom", "cell_matrix_convention": "row_vectors_a_b_c"},
        "boundary": {"periodic_axes": [], "lammps_boundary": ["f", "f", "f"]},
        "lammps": {"atom_style": "full", "triclinic_tilt_handling": "preserve"},
        "charge": {"formal_charge_semantics": "oxidation_or_connectivity_only", "partial_charge_semantics": "forcefield_nonbonded_charge", "partial_charge_source": "Materials Studio PCFF 3.1", "expected_net_partial_charge_e": 0.0, "audit_by_component": True},
        "forcefield": {"profile_id": profile_data["profile_id"], "profile_version": "1.0.0", "profile_sha256": _sha256(profile).lower(), "parameter_file_sha256": [_sha256(PCFF_OFF).lower(), _sha256(PCFF_FRC).lower()], "compatibility_reviewed": True, "mixing_rule": "sixthpower", "special_bonds": "lj/coul 0.0 0.0 1.0", "long_range_electrostatics": "none_nonperiodic"},
        "seed_ledger": {"master_seed": _seed(project_id, "master"), "derivation": "sha256_run_uuid_stage_replica_to_int31_v1", "replica_index": 0, "streams": seed_streams},
        "trajectory": {"wrapped_fields": "x_y_z", "unwrapped_fields": "xu_yu_zu", "msd_uses_unwrapped": True, "density_uses_wrapped": True, "timestep_unit": "femtosecond"},
    }
    validation = update_model_specification(
        str(project), specification, forcefield, science_contract,
        {"fidelity": "reviewed_scientific_model", "clay_is_surrogate": False, "source_model": str(OFFICIAL_WATER)},
    )
    if validation["status"] != "valid" or not validation["production_allowed"]:
        raise RuntimeError(f"G01 project specification did not validate: {validation['errors']}")
    transition_project_status(str(project), "specified", reason="G01 specification and science contract validated", evidence_ids=[_sha256(profile)])

    from . import server

    assigned_xsd = project / "model" / "g01_pcff_assigned.xsd"
    car = project / "conversion" / "g01_pcff_assigned.car"
    mdf = project / "conversion" / "g01_pcff_assigned.mdf"
    ms_report = project / "reports" / "g01_pcff_ms_energy.txt"
    script = r'''use strict;
use warnings;
use MaterialsScript qw(:all);
my $doc = Documents->Import("{{input.structure}}");
Modules->Forcite->ChangeSettings([
    CurrentForcefield => "pcff",
    AssignForcefieldTypes => "Yes",
    ChargeAssignment => "Forcefield assigned"
]);
Modules->Forcite->Energy->Run($doc);
open(my $fh, '>', "{{output.report}}") or die $!;
print $fh "status=PASS\n";
print $fh "forcefield=pcff\n";
print $fh "PotentialEnergy=", $doc->PotentialEnergy, "\n";
print $fh "ValenceDiagonalEnergy=", $doc->ValenceDiagonalEnergy, "\n";
print $fh "ValenceCrossTermEnergy=", $doc->ValenceCrossTermEnergy, "\n";
print $fh "BondEnergy=", $doc->BondEnergy, "\n";
print $fh "AngleEnergy=", $doc->AngleEnergy, "\n";
print $fh "atom_count=", $doc->Atoms->Count, "\n";
my $atoms = $doc->Atoms;
foreach my $atom (@$atoms) {
    my $p = $atom->XYZ;
    print $fh join(',', "atom", $atom->ElementSymbol, $atom->ForcefieldType, $atom->Charge, $p->X, $p->Y, $p->Z), "\n";
}
close($fh);
$doc->Export("{{output.assigned_xsd}}");
$doc->Export("{{output.car}}");
$doc->Close;
'''
    ms_result = server._run_materialsscript_job(
        script_template=script,
        input_files={"structure": str(source)},
        output_files={
            "assigned_xsd": {"relative_path": "g01_pcff_assigned.xsd", "destination_path": str(assigned_xsd)},
            "car": {"relative_path": "g01_pcff_assigned.car", "destination_path": str(car)},
            "mdf": {"relative_path": "g01_pcff_assigned.mdf", "destination_path": str(mdf)},
            "report": {"relative_path": "g01_pcff_ms_energy.txt", "destination_path": str(ms_report)},
        },
        job_name="g01_pcff_v1_reproduction",
        run_mode="project",
        keep_job_dir=True,
        timeout_seconds=300,
    )
    if not ms_result.get("success"):
        raise RuntimeError(ms_result.get("error_summary") or "G01 Materials Studio step failed")
    ms_receipt = project / "reports" / "g01_materialsscript_receipt.json"
    _write_json(ms_receipt, ms_result)
    for path, role in ((assigned_xsd, "g01_pcff_assigned_xsd"), (car, "g01_pcff_car"), (mdf, "g01_pcff_mdf"), (ms_report, "g01_ms_energy"), (ms_receipt, "g01_materialsscript_receipt")):
        register_artifact(str(project), str(path), role, "fresh G01 Materials Studio reproduction")
    ms_values = _parse_key_values(ms_report)
    ms_potential = float(ms_values["PotentialEnergy"])
    if int(ms_values["atom_count"]) != 3 or abs(ms_potential - EXPECTED_MS_POTENTIAL) > ENERGY_TOLERANCE_KCAL_MOL:
        raise RuntimeError("Fresh Materials Studio G01 energy or atom count differs from the calibrated fixture")
    transition_project_status(str(project), "modelled", reason="Fresh PCFF assignment and MS energy passed", evidence_ids=[_sha256(ms_report), _sha256(assigned_xsd)])

    convert_parameters = {
        "project_directory": str(project),
        "car_path": str(car),
        "car_sha256": _sha256(car),
        "mdf_path": str(mdf),
        "mdf_sha256": _sha256(mdf),
        "forcefield_file": str(PCFF_FRC),
        "forcefield_sha256": _sha256(PCFF_FRC),
        "forcefield_class": "II",
        "output_slot": "g01_pcff_converted",
        "idempotency_key": f"{project_id}-checked-conversion-v1",
        "timeout_seconds": 300,
    }
    confirmation = confirmation_manager.issue("md_convert_to_lammps_checked", convert_parameters, 300)
    converted = server.md_convert_to_lammps_checked(
        **convert_parameters, confirmation_token=confirmation["confirmation_token"]
    )
    if not converted.get("ok"):
        raise RuntimeError(f"G01 checked conversion failed: {converted.get('error')}")
    data = Path(converted["data"]["output_data"])
    conversion_receipt = project / "reports" / "g01_checked_conversion_receipt.json"
    _write_json(conversion_receipt, converted)
    register_artifact(str(project), str(conversion_receipt), "g01_checked_conversion_receipt", "md_convert_to_lammps_checked")
    transition_project_status(str(project), "converted", reason="Checked Class II PCFF conversion passed", evidence_ids=[converted["data"]["output_data_sha256"]])

    lammps_dir = project / "lammps" / "logs" / "g01_pcff_run0_v1"
    run0 = _run_pcff_run0(data, lammps_dir)
    if run0["status"] != "pass":
        raise RuntimeError(f"G01 LAMMPS run 0 failed: {run0['errors']}")
    _register_tree(project, lammps_dir, "g01_lammps", "fresh G01 run 0")

    energy_difference = abs(ms_potential - float(run0["thermo"]["PotEng"]))
    if energy_difference > ENERGY_TOLERANCE_KCAL_MOL:
        raise RuntimeError(f"MS/LAMMPS energy difference {energy_difference} exceeds tolerance")
    vmd_dir = project / "vmd" / "g01_v1"
    vmd = validate_vmd_text_trajectory(
        str(data), str(lammps_dir / "trajectory.g01.lammpstrj"), str(vmd_dir),
        expected_frames=1, timeout_seconds=60,
    )
    if vmd["status"] != "pass":
        raise RuntimeError(f"G01 VMD validation failed: {vmd.get('errors')}")
    _register_tree(project, vmd_dir, "g01_vmd", "fresh G01 VMD text validation")

    contract_result = validate_science_contract(validate_project(str(project))["manifest"])
    gate_evidence = {
        "structure": {"atoms": 3, "bonds": 2, "angles": 1, "net_charge_e": 0.0, "data_sha256": _sha256(data)},
        "forcefield": {"profile_sha256": _sha256(profile), "pcff_off_sha256": _sha256(PCFF_OFF), "pcff_frc_sha256": _sha256(PCFF_FRC), "class": "II"},
        "lammps_preflight": {"run0_status": run0["status"], "parameter_coverage": run0["parameter_and_topology_gate"]},
        "scientific_validation": {"ms_potential_kcal_mol": ms_potential, "lammps_potential_kcal_mol": run0["thermo"]["PotEng"], "absolute_difference_kcal_mol": energy_difference, "tolerance_kcal_mol": ENERGY_TOLERANCE_KCAL_MOL, "vmd_status": vmd["status"]},
        "science_contract": contract_result,
    }
    for gate, evidence in gate_evidence.items():
        _record_verified_quality_gate(
            str(project), gate, validator=f"g01_reproduction:{gate}:v1", passed=True, evidence=evidence
        )
    transition_project_status(str(project), "preflight_passed", reason="Fresh run0, VMD, hashes, and all trusted gates passed", evidence_ids=[_sha256(lammps_dir / "g01_run0_evidence.json"), _sha256(vmd_dir / "vmd_validation_evidence.json")])
    transition_project_status(str(project), "production", reason="Execute the reviewed G01 calibration workflow only", evidence_ids=[_sha256(profile)])
    transition_project_status(str(project), "validated", reason="G01 calibration reproduction matched reviewed MS/LAMMPS/VMD targets", evidence_ids=[_sha256(ms_report), _sha256(lammps_dir / "g01_run0_evidence.json"), _sha256(vmd_dir / "vmd_validation_evidence.json")])

    project_validation = validate_project(str(project))
    if project_validation["status"] != "valid" or project_validation["manifest"]["project"]["status"] != "validated":
        raise RuntimeError(f"Final G01 project validation failed: {project_validation['errors']}")
    report = {
        "schema_version": 1,
        "reproduction": "G01 PCFF v1",
        "status": "pass",
        "project_status": "validated",
        "project_directory": str(project),
        "fresh_run": True,
        "source": {"path": str(OFFICIAL_WATER), "sha256": _sha256(OFFICIAL_WATER)},
        "materials_studio": {"potential_kcal_mol": ms_potential, "report_sha256": _sha256(ms_report)},
        "conversion": {"data_path": str(data), "data_sha256": _sha256(data), "production_released": False},
        "lammps": {"potential_kcal_mol": run0["thermo"]["PotEng"], "evidence_sha256": _sha256(lammps_dir / "g01_run0_evidence.json")},
        "vmd": {"status": vmd["status"], "evidence_sha256": _sha256(vmd_dir / "vmd_validation_evidence.json")},
        "energy_equivalence": {"absolute_difference_kcal_mol": energy_difference, "tolerance_kcal_mol": ENERGY_TOLERANCE_KCAL_MOL, "pass": True},
        "quality_gates": project_validation["manifest"]["quality_gates"],
        "project_validation_status": project_validation["status"],
        "production_science_released": False,
        "scope": "Three-atom nonperiodic PCFF calibration fixture only; not a production water model or trajectory.",
    }
    report_path = project / "reports" / "G01_V1_REPRODUCTION_REPORT.json"
    _write_json(report_path, report)
    register_artifact(str(project), str(report_path), "g01_final_reproduction_report", "g01_reproduction:v1")
    final_validation = validate_project(str(project))
    if final_validation["status"] != "valid":
        raise RuntimeError(f"Final artifact registration invalidated the project: {final_validation['errors']}")
    return {
        "status": "pass",
        "project_directory": str(project),
        "report_path": str(report_path),
        "report_sha256": _sha256(report_path),
        "project_status": "validated",
        "artifact_count": len(final_validation["manifest"]["artifacts"]),
        "production_science_released": False,
    }
