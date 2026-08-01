from __future__ import annotations

import hashlib
import argparse
import json
import re
from pathlib import Path
from typing import Any

from .geology_modeling import inspect_xsd_geometry


PLAN_SCHEMA_VERSION = 1
CASTEP_DOC = "castepscripting/apicastepgeometryoptimization.htm"

_CASTEP_TERMS = (
    "castep",
    "dft",
    "first principles",
    "first-principles",
    "electronic structure",
    "density functional",
    "第一性原理",
    "电子结构",
    "量子力学优化",
)

_FORCITE_TERMS = ("forcite", "molecular dynamics", "geometry optimization", "分子动力学", "力场优化")
_LAMMPS_TERMS = ("lammps", "large-scale md", "大规模分子动力学")

_ALLOWED_CONTEXT_KEYS = {
    "schema_version",
    "engine",
    "task",
    "purpose",
    "input_sha256",
    "electronic_character",
    "magnetism",
    "cell_optimization",
    "dispersion",
    "accuracy",
    "xc_functional",
    "pseudopotentials",
    "kpoint_derivation",
    "kpoint_quality",
    "kpoint_separation",
    "energy_cutoff_ev",
    "smearing_ev",
    "cores",
    "convergence_evidence",
}


def _decision(context: dict[str, Any], name: str, default: Any = None) -> tuple[Any, str]:
    raw = context.get(name, default)
    if isinstance(raw, dict):
        extra = set(raw) - {"value", "source"}
        if extra or "value" not in raw:
            raise ValueError(f"calculation_context.{name} must contain only value and source")
        source = raw.get("source")
        if not isinstance(source, str) or not source.strip():
            raise ValueError(f"calculation_context.{name}.source must be a non-empty string")
        return raw["value"], source.strip()
    return raw, "caller" if name in context else "planner_default"


def _choice(name: str, value: Any, allowed: set[str]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(f"{name} must be one of {', '.join(sorted(allowed))}")
    return value


def _optional_positive(name: str, value: Any, maximum: float) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric or null")
    number = float(value)
    if not 0.0 < number <= maximum:
        raise ValueError(f"{name} must be greater than 0 and at most {maximum}")
    return number


def _structure_facts(input_structure: str | None, expected_sha256: str | None) -> tuple[dict[str, Any] | None, list[dict[str, str]]]:
    blockers: list[dict[str, str]] = []
    if not input_structure:
        blockers.append({"code": "INPUT_STRUCTURE_REQUIRED", "detail": "A CASTEP plan requires an exact XSD input."})
        return None, blockers
    path = Path(input_structure).resolve()
    if not path.is_file() or path.suffix.lower() != ".xsd":
        blockers.append({"code": "VALID_XSD_REQUIRED", "detail": f"CASTEP planning requires an existing XSD: {path}"})
        return None, blockers
    actual = hashlib.sha256(path.read_bytes()).hexdigest().upper()
    if expected_sha256 is None:
        blockers.append({"code": "INPUT_HASH_REQUIRED", "detail": f"Bind the plan to SHA-256 {actual}."})
    elif not isinstance(expected_sha256, str) or re.fullmatch(r"[0-9A-Fa-f]{64}", expected_sha256) is None:
        raise ValueError("calculation_context.input_sha256 must contain 64 hexadecimal characters")
    elif actual != expected_sha256.upper():
        blockers.append({"code": "INPUT_HASH_MISMATCH", "detail": f"Expected {expected_sha256.upper()}, got {actual}."})
    geometry = inspect_xsd_geometry(path)
    return {
        "path": str(path),
        "sha256": actual,
        "periodic_dimension": geometry["periodic_dimension"],
        "runtime_atom_count": geometry["atom_count"],
        "asymmetric_atom_count": geometry["asymmetric_atom_count"],
        "elements": geometry["elements"],
        "formal_charge_e": geometry["formal_charge_e"],
        "cell_volume_A3": geometry["cell_volume_A3"],
    }, blockers


def build_adaptive_calculation_plan(
    *,
    request: str,
    input_structure: str | None,
    calculation_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a fail-closed plan; never execute or silently invent science choices."""

    if not isinstance(request, str) or not request.strip():
        raise ValueError("request must be a non-empty string")
    context = dict(calculation_context or {})
    unknown = set(context) - _ALLOWED_CONTEXT_KEYS
    if unknown:
        raise ValueError(f"Unknown calculation_context keys: {', '.join(sorted(unknown))}")
    if context.get("schema_version", PLAN_SCHEMA_VERSION) != PLAN_SCHEMA_VERSION:
        raise ValueError(f"calculation_context.schema_version must be {PLAN_SCHEMA_VERSION}")

    requested_engine, engine_source = _decision(context, "engine", "auto")
    requested_engine = _choice("engine", requested_engine, {"auto", "CASTEP", "Forcite", "LAMMPS"})
    lowered = request.lower()
    if requested_engine == "auto":
        if any(term in lowered for term in _CASTEP_TERMS):
            engine = "CASTEP"
        elif any(term in lowered for term in _LAMMPS_TERMS):
            engine = "LAMMPS"
        elif any(term in lowered for term in _FORCITE_TERMS) or re.search(r"\bmd\b", lowered):
            engine = "Forcite"
        else:
            engine = "unresolved"
        engine_source = "request_terms" if engine != "unresolved" else "insufficient_context"
    else:
        engine = requested_engine

    if engine != "CASTEP":
        return {
            "schema_version": PLAN_SCHEMA_VERSION,
            "status": "not_applicable" if engine != "unresolved" else "needs_context",
            "engine": engine,
            "engine_source": engine_source,
            "execution_allowed": False,
            "blockers": [] if engine != "unresolved" else [{
                "code": "ENGINE_SELECTION_REQUIRED",
                "detail": "Select CASTEP, Forcite, or LAMMPS from the scientific objective and scale.",
            }],
        }

    task, task_source = _decision(context, "task", "geometry_optimization")
    task = _choice("task", task, {"geometry_optimization", "energy"})
    purpose, purpose_source = _decision(context, "purpose", "runtime_preflight")
    purpose = _choice("purpose", purpose, {"runtime_preflight", "screening", "preliminary", "research"})
    input_hash, _ = _decision(context, "input_sha256", None)
    facts, blockers = _structure_facts(input_structure, input_hash)

    electronic, electronic_source = _decision(context, "electronic_character", "unknown")
    electronic = _choice("electronic_character", electronic, {"unknown", "insulator", "semiconductor", "metal"})
    magnetism, magnetism_source = _decision(context, "magnetism", "unknown")
    magnetism = _choice("magnetism", magnetism, {"unknown", "nonmagnetic", "collinear", "spin_screen"})
    cell, cell_source = _decision(context, "cell_optimization", "unknown")
    cell = _choice("cell_optimization", cell, {"unknown", "none", "full", "fixed_volume", "fixed_shape"})
    dispersion, dispersion_source = _decision(context, "dispersion", "off")
    dispersion = _choice("dispersion", dispersion, {"off", "TS", "Grimme", "OBS", "MBD*"})
    accuracy, accuracy_source = _decision(context, "accuracy", "Express" if purpose in {"runtime_preflight", "screening"} else "Medium")
    accuracy = _choice("accuracy", accuracy, {"Express", "Coarse", "Medium", "Fine", "Ultra-fine"})
    xc, xc_source = _decision(context, "xc_functional", "PBE")
    xc = _choice("xc_functional", xc, {"LDA", "PBE", "RPBE", "PW91", "WC", "PBESOL", "BLYP", "PBE0", "B3LYP", "HSE03", "HSE06", "RSCAN"})
    pseudo, pseudo_source = _decision(context, "pseudopotentials", "OTFG ultrasoft")
    pseudo = _choice("pseudopotentials", pseudo, {"OTFG ultrasoft", "OTFG norm-conserving", "Norm-conserving", "Ultrasoft", "High throughput"})
    kderivation, kderivation_source = _decision(context, "kpoint_derivation", "Quality")
    kderivation = _choice("kpoint_derivation", kderivation, {"Quality", "Separation", "Gamma"})
    kquality, kquality_source = _decision(context, "kpoint_quality", "Coarse" if purpose in {"runtime_preflight", "screening"} else "Medium")
    kquality = _choice("kpoint_quality", kquality, {"Coarse", "Medium", "Fine"})
    kseparation, kseparation_source = _decision(context, "kpoint_separation", None)
    kseparation = _optional_positive("kpoint_separation", kseparation, 10.0)
    cutoff, cutoff_source = _decision(context, "energy_cutoff_ev", None)
    cutoff = _optional_positive("energy_cutoff_ev", cutoff, 99999.0)
    smearing, smearing_source = _decision(context, "smearing_ev", None)
    smearing = _optional_positive("smearing_ev", smearing, 10.0)
    cores, cores_source = _decision(context, "cores", 4)
    if isinstance(cores, bool) or not isinstance(cores, int) or not 1 <= cores <= 12:
        raise ValueError("cores must be an integer from 1 to the reviewed local ceiling of 12")
    convergence, convergence_source = _decision(context, "convergence_evidence", [])
    if not isinstance(convergence, list) or any(not isinstance(item, str) or not item.strip() for item in convergence):
        raise ValueError("convergence_evidence must be a list of non-empty evidence identifiers")

    if task != "geometry_optimization":
        blockers.append({"code": "TASK_NOT_GENERATED", "detail": "The current governed PL generator implements geometry optimization only."})
    if purpose != "runtime_preflight":
        if electronic == "unknown":
            blockers.append({"code": "ELECTRONIC_CHARACTER_REQUIRED", "detail": "Confirm insulator, semiconductor, or metal before choosing occupancy and smearing."})
        if magnetism == "unknown":
            blockers.append({"code": "MAGNETISM_REQUIRED", "detail": "Confirm nonmagnetic, collinear, or spin-screen treatment."})
        if cell == "unknown":
            blockers.append({"code": "CELL_POLICY_REQUIRED", "detail": "Choose atomic-only or an explicit cell-optimization policy."})
    if electronic == "metal" and smearing is None:
        blockers.append({"code": "METAL_SMEARING_REQUIRED", "detail": "Provide a reviewed smearing width for a metallic calculation."})
    if kderivation == "Separation" and kseparation is None:
        blockers.append({"code": "KPOINT_SEPARATION_REQUIRED", "detail": "KPointDerivation=Separation requires kpoint_separation."})
    if purpose == "research" and not convergence:
        blockers.append({"code": "CONVERGENCE_EVIDENCE_REQUIRED", "detail": "Research execution requires cutoff and k-point convergence evidence."})

    spin_treatment = None
    if magnetism == "nonmagnetic":
        spin_treatment = "Non-polarized"
    elif magnetism in {"collinear", "spin_screen"}:
        spin_treatment = "Collinear"

    settings = {
        "Quality": accuracy,
        "XCFunctional": xc,
        "Pseudopotentials": pseudo,
        "UseDFTD": "No" if dispersion == "off" else "Yes",
        "DFTDMethod": None if dispersion == "off" else dispersion,
        "UseCustomEnergyCutoff": "No" if cutoff is None else "Yes",
        "EnergyCutoff": cutoff,
        "KPointDerivation": kderivation,
        "KPointQuality": kquality if kderivation == "Quality" else None,
        "KPointSeparation": kseparation if kderivation == "Separation" else None,
        "Smearing": smearing,
        "SpinTreatment": spin_treatment,
        "CellOptimization": {"unknown": None, "none": "None", "full": "Full", "fixed_volume": "Fixed Volume", "fixed_shape": "Fixed Shape"}[cell],
        "EstimatedCompressibility": "Hard" if cell != "none" else None,
    }
    origins = {
        "engine": engine_source,
        "task": task_source,
        "purpose": purpose_source,
        "electronic_character": electronic_source,
        "magnetism": magnetism_source,
        "cell_optimization": cell_source,
        "dispersion": dispersion_source,
        "accuracy": accuracy_source,
        "xc_functional": xc_source,
        "pseudopotentials": pseudo_source,
        "kpoint_derivation": kderivation_source,
        "kpoint_quality": kquality_source,
        "kpoint_separation": kseparation_source,
        "energy_cutoff_ev": cutoff_source,
        "smearing_ev": smearing_source,
        "cores": cores_source,
        "convergence_evidence": convergence_source,
    }
    execution_blockers = list(blockers)
    execution_blockers.append({
        "code": "CASTEP_CALCULATION_CAPABILITY_UNVERIFIED",
        "detail": "MCP 1.2.2 verifies adaptive package generation and runtime-only preflight, not CASTEP calculation or result parsing.",
    })
    return {
        "schema_version": PLAN_SCHEMA_VERSION,
        "status": "ready_for_runtime_preflight" if facts and not blockers else "blocked",
        "engine": "CASTEP",
        "task": task,
        "purpose": purpose,
        "structure_facts": facts,
        "scientific_classification": {"electronic_character": electronic, "magnetism": magnetism},
        "settings": settings,
        "setting_origins": origins,
        "resources": {"cores": cores, "local_logical_processor_ceiling": 12, "max_parallel_jobs": 1},
        "documentation_evidence": [{"path": CASTEP_DOC, "role": "MS 2023 setting names, domains, defaults, and task API"}],
        "preflight_blockers": blockers,
        "execution_blockers": execution_blockers,
        "execution_allowed": False,
        "recommended_tool": "ms_prepare_castep_pl_package",
        "notes": [
            "Official defaults are API evidence, not material-specific convergence evidence.",
            "Omitted cutoff uses the selected Quality tier; a custom cutoff requires explicit evidence.",
            "No Gateway is selected and no calculation is submitted by this plan.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a no-execution adaptive calculation plan.")
    parser.add_argument("--request", required=True)
    parser.add_argument("--input-structure")
    parser.add_argument("--context-json", type=Path)
    args = parser.parse_args()
    context = None
    if args.context_json is not None:
        context = json.loads(args.context_json.read_text(encoding="utf-8"))
    plan = build_adaptive_calculation_plan(
        request=args.request,
        input_structure=args.input_structure,
        calculation_context=context,
    )
    # ASCII escaping preserves exact Unicode values through legacy Windows
    # console code pages while remaining valid JSON for all clients.
    print(json.dumps(plan, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
