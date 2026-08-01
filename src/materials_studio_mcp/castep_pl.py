from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from .geology_modeling import inspect_xsd_geometry


GENERATOR_SOURCE = "https://github.com/DrYe1109/MS-CASTEP-PL-Generator"
GENERATOR_REVISION = "bd1834efcf772f3c6b5d07d1aa06b471a055dd79+ms-mcp.1.2.2"

_ADAPTIVE_SETTING_VALUES = {
    "Quality": {"Express", "Coarse", "Medium", "Fine", "Ultra-fine"},
    "XCFunctional": {"LDA", "PBE", "RPBE", "PW91", "WC", "PBESOL", "BLYP", "PBE0", "B3LYP", "HSE03", "HSE06", "RSCAN"},
    "Pseudopotentials": {"OTFG ultrasoft", "OTFG norm-conserving", "Norm-conserving", "Ultrasoft", "High throughput"},
    "UseDFTD": {"No", "Yes"},
    "DFTDMethod": {"TS", "Grimme", "OBS", "MBD*"},
    "UseCustomEnergyCutoff": {"No", "Yes"},
    "KPointDerivation": {"Quality", "Separation", "Gamma"},
    "KPointQuality": {"Coarse", "Medium", "Fine"},
    "SpinTreatment": {"Non-polarized", "Collinear"},
    "CellOptimization": {"None", "Full", "Fixed Volume", "Fixed Shape"},
    "EstimatedCompressibility": {"Soft", "Medium", "Hard"},
}
_ADAPTIVE_NUMERIC_SETTINGS = {
    "EnergyCutoff": 99999.0,
    "KPointSeparation": 10.0,
    "Smearing": 10.0,
}


def validate_adaptive_plan(plan: dict[str, Any], source: dict[str, Any], cores: int) -> dict[str, Any]:
    if not isinstance(plan, dict) or plan.get("schema_version") != 1:
        raise ValueError("adaptive_plan must use schema_version 1")
    if plan.get("engine") != "CASTEP" or plan.get("task") != "geometry_optimization":
        raise ValueError("adaptive_plan must target CASTEP geometry_optimization")
    if plan.get("execution_allowed") is not False:
        raise ValueError("adaptive_plan execution_allowed must remain false until CASTEP calculation is verified")
    facts = plan.get("structure_facts")
    if not isinstance(facts, dict) or facts.get("sha256", "").upper() != source["sha256"]:
        raise ValueError("adaptive_plan structure hash does not match the input XSD")
    resources = plan.get("resources")
    if not isinstance(resources, dict) or resources.get("cores") != cores:
        raise ValueError("adaptive_plan resources.cores must match the requested cores")
    raw_settings = plan.get("settings")
    if not isinstance(raw_settings, dict):
        raise ValueError("adaptive_plan.settings must be an object")
    allowed = set(_ADAPTIVE_SETTING_VALUES) | set(_ADAPTIVE_NUMERIC_SETTINGS)
    unknown = set(raw_settings) - allowed
    if unknown:
        raise ValueError(f"adaptive_plan contains unknown CASTEP settings: {', '.join(sorted(unknown))}")
    cleaned: dict[str, Any] = {}
    for name, values in _ADAPTIVE_SETTING_VALUES.items():
        value = raw_settings.get(name)
        if value is None:
            continue
        if value not in values:
            raise ValueError(f"adaptive_plan setting {name} has an unsupported value")
        cleaned[name] = value
    for name, maximum in _ADAPTIVE_NUMERIC_SETTINGS.items():
        value = raw_settings.get(name)
        if value is None:
            continue
        cleaned[name] = _positive_finite(name, value, maximum)
    if cleaned.get("UseDFTD") == "Yes" and "DFTDMethod" not in cleaned:
        raise ValueError("adaptive_plan UseDFTD=Yes requires DFTDMethod")
    if cleaned.get("UseCustomEnergyCutoff") == "Yes" and "EnergyCutoff" not in cleaned:
        raise ValueError("adaptive_plan custom cutoff requires EnergyCutoff")
    if cleaned.get("KPointDerivation") == "Separation" and "KPointSeparation" not in cleaned:
        raise ValueError("adaptive_plan KPointDerivation=Separation requires KPointSeparation")
    return cleaned


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def safe_name(value: str, max_length: int = 40) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip()).strip("._")
    if not cleaned:
        raise ValueError("calculation_name is empty after ASCII sanitization")
    if len(cleaned) <= max_length:
        return cleaned
    digest = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:8]
    return f"{cleaned[: max_length - 9]}_{digest}"


def inspect_xsd(path: Path, expected_sha256: str) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"XSD not found: {path}")
    if path.suffix.lower() != ".xsd":
        raise ValueError("input_xsd must use the .xsd suffix")
    size = path.stat().st_size
    if size < 256:
        raise ValueError(f"XSD is unexpectedly small ({size} bytes): {path}")
    if not isinstance(expected_sha256, str) or re.fullmatch(r"[0-9A-Fa-f]{64}", expected_sha256) is None:
        raise ValueError("input_sha256 must contain exactly 64 hexadecimal characters")
    actual_sha256 = sha256_file(path)
    if actual_sha256 != expected_sha256.upper():
        raise ValueError(
            f"Input XSD SHA-256 mismatch: expected {expected_sha256.upper()}, got {actual_sha256}"
        )
    raw = path.read_bytes()
    text = raw.decode("utf-8", errors="ignore")
    if "<XSD" not in text:
        raise ValueError(f"file does not contain a Materials Studio XSD root: {path}")
    xml_atom_count = len(re.findall(r"<Atom3d\b", text))
    if xml_atom_count < 1:
        raise ValueError(f"XSD contains no Atom3d entries: {path}")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ValueError(f"XSD XML is not well formed: {path}") from exc
    independent_atoms = [
        item
        for item in root.iter()
        if item.tag.rsplit("}", 1)[-1] == "Atom3d" and item.get("ImageOf") is None
    ]
    if not independent_atoms:
        raise ValueError(f"XSD contains no independent Atom3d entries: {path}")
    has_periodic_group = any(
        item.tag.rsplit("}", 1)[-1] in {"SpaceGroup", "PlaneGroup"}
        for item in root.iter()
    )
    if has_periodic_group:
        geometry = inspect_xsd_geometry(path)
        runtime_atom_count = int(geometry["atom_count"])
        atom_count_method = "verified_space_group_unit_cell_expansion"
    else:
        runtime_atom_count = len(independent_atoms)
        atom_count_method = "non_image_atom3d_count"
    return {
        "source_name": path.name,
        "bytes": size,
        "xml_atom3d_entries": xml_atom_count,
        "independent_atom3d_entries": len(independent_atoms),
        "runtime_atom_count": runtime_atom_count,
        "atom_count_method": atom_count_method,
        "sha256": actual_sha256,
    }


def _positive_finite(name: str, value: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    number = float(value)
    if not math.isfinite(number) or not 0 < number <= maximum:
        raise ValueError(f"{name} must be finite and between 0 and {maximum}")
    return number


def validate_settings(
    *,
    spins: list[int],
    cores: int,
    cutoff: float,
    max_scf_cycles: int,
    max_geometry_iterations: int,
    scf_convergence: float,
    force_convergence: float,
    dispersion_method: str,
    spin_mode: str,
    density_mixing_amplitude: float,
    spin_mixing_amplitude: float,
    diis_history: int,
    smearing: float,
    optimization_algorithm: str,
) -> dict[str, Any]:
    if not isinstance(spins, list) or not spins:
        raise ValueError("spins must be a non-empty array")
    if len(spins) > 32:
        raise ValueError("spins may contain at most 32 values")
    if any(isinstance(spin, bool) or not isinstance(spin, int) or not 0 <= spin <= 256 for spin in spins):
        raise ValueError("each spin must be an integer between 0 and 256")
    if len(set(spins)) != len(spins):
        raise ValueError("spins must not contain duplicate values")
    integer_limits = {
        "cores": (cores, 1, 4096),
        "max_scf_cycles": (max_scf_cycles, 1, 10000),
        "max_geometry_iterations": (max_geometry_iterations, 1, 10000),
        "diis_history": (diis_history, 1, 100),
    }
    for name, (value, minimum, maximum) in integer_limits.items():
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")
    if dispersion_method not in {"TS", "Grimme", "OBS"}:
        raise ValueError("dispersion_method must be one of TS, Grimme, or OBS")
    if spin_mode not in {"fixed", "relaxed"}:
        raise ValueError("spin_mode must be fixed or relaxed")
    if optimization_algorithm not in {"BFGS", "LBFGS"}:
        raise ValueError("optimization_algorithm must be BFGS or LBFGS")
    return {
        "spins": list(spins),
        "cores": cores,
        "cutoff": _positive_finite("cutoff", cutoff, 5000.0),
        "max_scf_cycles": max_scf_cycles,
        "max_geometry_iterations": max_geometry_iterations,
        "scf_convergence": _positive_finite("scf_convergence", scf_convergence, 1.0),
        "force_convergence": _positive_finite("force_convergence", force_convergence, 100.0),
        "dispersion_method": dispersion_method,
        "spin_mode": spin_mode,
        "density_mixing_amplitude": _positive_finite(
            "density_mixing_amplitude", density_mixing_amplitude, 1.0
        ),
        "spin_mixing_amplitude": _positive_finite(
            "spin_mixing_amplitude", spin_mixing_amplitude, 1.0
        ),
        "diis_history": diis_history,
        "smearing": _positive_finite("smearing", smearing, 10.0),
        "optimization_algorithm": optimization_algorithm,
    }


def _adaptive_settings_lines(settings: dict[str, Any], initial_spin_expression: str) -> list[str]:
    ordered = [
        "Quality", "XCFunctional", "Pseudopotentials", "UseDFTD", "DFTDMethod",
        "UseCustomEnergyCutoff", "EnergyCutoff", "KPointDerivation", "KPointQuality",
        "KPointSeparation", "Smearing", "SpinTreatment", "CellOptimization",
        "EstimatedCompressibility",
    ]
    lines: list[str] = []
    for name in ordered:
        if name not in settings:
            continue
        value = settings[name]
        rendered = json.dumps(value, ensure_ascii=True) if isinstance(value, str) else str(value)
        lines.append(f'    "{name}" => {rendered},')
    if settings.get("SpinTreatment") == "Collinear":
        lines.extend([
            '    "UseFormalSpin" => "No",',
            f'    "InitialSpin" => {initial_spin_expression},',
            '    "OptimizeTotalSpin" => "No",',
        ])
    return lines


def render_pl(
    *,
    model_name: str,
    task_name: str,
    spin: int,
    expected_atoms: int,
    settings: dict[str, Any],
    allow_local: bool,
) -> str:
    local_guard = "" if allow_local else r'''
die "LOCAL EXECUTION BLOCKED: choose a remote Gateway in Run on Server.\n"
    if $^O =~ /MSWin32/i;
'''
    optimize_spin = "Yes" if settings["spin_mode"] == "relaxed" else "No"
    adaptive = settings.get("adaptive_castep_settings")
    if adaptive:
        calculation_settings = "\n".join(_adaptive_settings_lines(adaptive, "$initial_spin"))
    else:
        calculation_settings = f'''    "XCFunctional" => "PBE",
    "Pseudopotentials" => "OTFG ultrasoft",
    "UseDFTD" => "Yes",
    "DFTDMethod" => "{settings["dispersion_method"]}",
    "UseCustomEnergyCutoff" => "Yes",
    "EnergyCutoff" => {settings["cutoff"]},
    "KPointDerivation" => "Gamma",
    "Smearing" => {settings["smearing"]},
    "CellOptimization" => "None",
    "SpinTreatment" => "Collinear",
    "UseFormalSpin" => "No",
    "InitialSpin" => $initial_spin,
    "OptimizeTotalSpin" => "{optimize_spin}",'''
    return f'''#!perl
use strict;
use warnings;
use MaterialsScript qw(:all);

$ENV{{DSD_NumProc}} = {settings["cores"]};

my $model = "{model_name}";
my $calc = "{task_name}";
my $initial_spin = {spin};
my $expected_atoms = {expected_atoms};

my $source = eval {{ $Documents{{$model}} }};
if (!$source) {{
    $source = eval {{ Documents->Import($model) }};
}}
die "Input document lookup/import failed: $model; $@" if $@;
die "Input document not found: $model" unless $source;
my $atom_count = $source->UnitCell->Atoms->Count;
die "Input document is empty: $model" unless $atom_count > 0;
die "Unexpected atom count for $model: expected $expected_atoms, got $atom_count"
    unless $atom_count == $expected_atoms;
print "Validated input: $model; atoms=$atom_count; spin=$initial_spin; cores={settings["cores"]}\n";

if (($ENV{{MS_CASTEP_PL_PREFLIGHT_ONLY}} || "") eq "1") {{
    print "RESULT status=preflight_only calculation=$calc spin=$initial_spin atoms=$atom_count\n";
    exit 0;
}}

{local_guard}
my $work = $source->SaveAs("/$calc/in.xsd");
my $settings = Settings(
{calculation_settings}
    "MaximumSCFCycles" => {settings["max_scf_cycles"]},
    "EnergyTolerancesScope" => "Atom",
    "SCFConvergence" => {settings["scf_convergence"]},
    "DensityMixingScheme" => "Pulay",
    "DensityMixingAmplitude" => {settings["density_mixing_amplitude"]},
    "SpinMixingAmplitude" => {settings["spin_mixing_amplitude"]},
    "DIISHistory" => {settings["diis_history"]},
    "OptimizationAlgorithm" => "{settings["optimization_algorithm"]}",
    "MaxIterations" => {settings["max_geometry_iterations"]},
    "EnergyConvergence" => 0.00001,
    "ForceConvergence" => {settings["force_convergence"]},
    "DisplacementConvergence" => 0.001,
    "CalculateCharge" => "Hirshfeld",
    "CalculateSpin" => "Hirshfeld"
);

my $results = Modules->CASTEP->GeometryOptimization->Run($work, $settings);
eval {{ my $optimized = $results->Structure; $optimized->SaveAs("/$calc/opt.xsd") if $optimized; }};
print "WARNING: optimized structure export failed: $@\n" if $@;
eval {{ my $report = $results->Report; $report->SaveAs("/$calc/report.txt") if $report; }};
print "WARNING: report export failed: $@\n" if $@;
my $energy = "";
my $moment = "";
eval {{ $energy = $results->TotalEnergy; }};
eval {{ $moment = $results->TotalSpin; }};
print "RESULT status=completed calculation=$calc spin=$initial_spin atoms=$atom_count "
    . "energy=$energy final_moment=$moment\n";
'''


def _task_specs(base_calc: str, spins: list[int], cores: int) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    for spin in spins:
        task_name = safe_name(f"{base_calc}_s{spin}_{cores}c", max_length=48)
        tasks.append(
            {
                "task": task_name,
                "directory_name": task_name,
                "xsd_document": f"{base_calc}_s{spin}.xsd",
                "pl_document": f"run_{task_name}.pl",
                "initial_spin": spin,
                "cores": cores,
            }
        )
    return tasks


def prepare_castep_pl_package(
    *,
    input_xsd: Path,
    input_sha256: str,
    output_directory: Path,
    calculation_name: str,
    spins: list[int],
    cores: int = 4,
    cutoff: float = 326.5,
    max_scf_cycles: int = 500,
    max_geometry_iterations: int = 150,
    scf_convergence: float = 0.000002,
    force_convergence: float = 0.03,
    dispersion_method: str = "TS",
    spin_mode: str = "fixed",
    density_mixing_amplitude: float = 0.05,
    spin_mixing_amplitude: float = 0.08,
    diis_history: int = 5,
    smearing: float = 0.2,
    optimization_algorithm: str = "BFGS",
    allow_local: bool = False,
    dry_run: bool = True,
    adaptive_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(allow_local, bool) or not isinstance(dry_run, bool):
        raise ValueError("allow_local and dry_run must be booleans")
    source = inspect_xsd(input_xsd, input_sha256)
    settings = validate_settings(
        spins=spins,
        cores=cores,
        cutoff=cutoff,
        max_scf_cycles=max_scf_cycles,
        max_geometry_iterations=max_geometry_iterations,
        scf_convergence=scf_convergence,
        force_convergence=force_convergence,
        dispersion_method=dispersion_method,
        spin_mode=spin_mode,
        density_mixing_amplitude=density_mixing_amplitude,
        spin_mixing_amplitude=spin_mixing_amplitude,
        diis_history=diis_history,
        smearing=smearing,
        optimization_algorithm=optimization_algorithm,
    )
    adaptive_castep_settings = None
    if adaptive_plan is not None:
        adaptive_castep_settings = validate_adaptive_plan(adaptive_plan, source, cores)
        settings["adaptive_castep_settings"] = adaptive_castep_settings
        settings["legacy_profile_active"] = False
        settings["legacy_values_not_applied"] = {
            "cutoff": settings["cutoff"],
            "dispersion_method": settings["dispersion_method"],
            "smearing": settings["smearing"],
        }
        settings["cutoff"] = None
        settings["dispersion_method"] = None
        settings["smearing"] = None
    else:
        settings["legacy_profile_active"] = True
    base_calc = safe_name(calculation_name)
    task_specs = _task_specs(base_calc, spins, cores)
    preview = {
        "status": "dry_run" if dry_run else "prepared",
        "automatic_submission": False,
        "gateway_selected": False,
        "execution_started": False,
        "allow_local": allow_local,
        "source": source,
        "output_directory": str(output_directory),
        "settings": settings,
        "adaptive_plan": adaptive_plan,
        "scientific_execution_allowed": False if adaptive_plan is not None else None,
        "generator": {"source": GENERATOR_SOURCE, "revision": GENERATOR_REVISION},
        "tasks": task_specs,
        "next_actions": [
            "Run ms_castep_preflight_checked against the exact package manifest and task before any submission.",
            "Resolve the adaptive plan execution blockers before any Gateway selection or CASTEP submission."
            if adaptive_plan is not None
            else "Only after preflight passes, import the XSD/PL and manually select a reviewed Gateway and queue.",
        ],
    }
    if dry_run:
        preview["writes_performed"] = False
        return preview
    output_directory.mkdir(parents=True, exist_ok=False)
    try:
        task_results: list[dict[str, Any]] = []
        for spec in task_specs:
            task_dir = output_directory / spec["directory_name"]
            task_dir.mkdir()
            xsd_path = task_dir / spec["xsd_document"]
            pl_path = task_dir / spec["pl_document"]
            shutil.copy2(input_xsd, xsd_path)
            copied_xsd_sha256 = sha256_file(xsd_path)
            if copied_xsd_sha256 != source["sha256"]:
                raise RuntimeError(
                    "Copied XSD SHA-256 changed after source validation; refusing a time-of-check/time-of-use mismatch"
                )
            pl_path.write_text(
                render_pl(
                    model_name=spec["xsd_document"],
                    task_name=spec["task"],
                    spin=spec["initial_spin"],
                    expected_atoms=source["runtime_atom_count"],
                    settings=settings,
                    allow_local=allow_local,
                ),
                encoding="utf-8",
            )
            task_results.append(
                {
                    **spec,
                    "directory": str(task_dir),
                    "xsd_path": str(xsd_path),
                    "xsd_sha256": copied_xsd_sha256,
                    "pl_path": str(pl_path),
                    "pl_sha256": sha256_file(pl_path),
                    "expected_atoms": source["runtime_atom_count"],
                    "gateway": "USER_SELECTS_IN_MATERIALS_STUDIO",
                    "result_documents": ["opt.xsd", "report.txt"],
                }
            )
        manifest = {
            **preview,
            "status": "prepared",
            "writes_performed": True,
            "tasks": task_results,
        }
        manifest_path = output_directory / "package_manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        instructions_path = output_directory / "MANUAL_SUBMISSION.txt"
        submission_header = (
            "SUBMISSION BLOCKED BY ADAPTIVE PLAN\n\n"
            if adaptive_plan is not None
            else "MANUAL SUBMISSION REQUIRED\n\n"
        )
        adaptive_warning = (
            "This package is approved for runtime-only preflight, not CASTEP execution. "
            "Resolve every execution_blocker in package_manifest.json and regenerate a reviewed package before submission.\n\n"
            if adaptive_plan is not None
            else ""
        )
        instructions_path.write_text(
            submission_header
            + adaptive_warning
            + "For each task directory, import both the XSD and PL into the same Materials Studio "
            "project folder. Verify the XSD, open the PL, press Ctrl+F5, select the reviewed "
            "remote Gateway and queue, and confirm the requested core count. This package "
            "generator selected no Gateway and submitted no job. Run ms_castep_preflight_checked "
            "first when the Materials Studio MCP is available. Successful scripts return "
            "opt.xsd and report.txt and print 'RESULT status=completed'.\n",
            encoding="utf-8",
        )
        return {
            **manifest,
            "manifest_path": str(manifest_path),
            "manifest_sha256": sha256_file(manifest_path),
            "manual_submission_path": str(instructions_path),
            "manual_submission_sha256": sha256_file(instructions_path),
        }
    except Exception:
        shutil.rmtree(output_directory, ignore_errors=True)
        raise
