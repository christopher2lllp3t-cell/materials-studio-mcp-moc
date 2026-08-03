"""Read-only model-intake checks for controlled Materials Studio workflows.

The functions in this module deliberately distinguish *mechanical readiness*
from scientific validity.  They can locate local inputs and describe a safe
next step, but they never select a force field, create missing parameters, or
claim that a model is scientifically fit for production.
"""

from __future__ import annotations

from collections.abc import Callable
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any

from .geology_modeling import inspect_xsd_geometry, sha256_file
from .pipeline_config import load_pipeline_config, resolve_workspace_path


_MODEL_CLASSES = frozenset({
    "crystal_bulk", "surface_slab", "clay_mineral", "aqueous_electrolyte",
    "organic_condensed_phase", "polymer", "porous_framework", "nanopore", "custom",
})
_ENGINES = frozenset({"structure_only", "forcite", "lammps", "castep"})
_PURPOSES = frozenset({
    "build_only", "forcefield_assignment", "conversion", "energy",
    "geometry_optimization", "dynamics", "dft_single_point", "dft_geometry_optimization",
})
_STRUCTURE_FORMATS = frozenset({"cif", "xsd", "car_mdf", "pdb", "sdf", "mol2"})
_PERIODIC_MODEL_CLASSES = frozenset({
    "crystal_bulk", "surface_slab", "clay_mineral", "porous_framework", "nanopore",
})
_SHA256 = re.compile(r"^[0-9A-Fa-f]{64}$")
_SAFE_NAME = re.compile(r"^[^\x00-\x1f]{1,120}$")
_FRC_SUFFIXES = frozenset({".frc", ".rlb", ".dat"})
_STRUCTURE_SUFFIXES = frozenset({".cif", ".xsd", ".car", ".mdf", ".pdb", ".sdf", ".mol2"})


# These are discovery labels only.  They are intentionally not a suitability
# table: the presence of a local file never establishes that it is appropriate
# for the caller's material, state point, charge model, or target observable.
_LOCAL_FRC_LABELS = {
    "clayff.frc": "ClayFF candidate",
    "compass_published.frc": "COMPASS published candidate",
    "pcff.frc": "PCFF candidate",
    "cvff.frc": "CVFF candidate",
    "cvff_aug.frc": "CVFF augmented candidate",
    "cff91.frc": "CFF91 candidate",
    "oplsaa.frc": "OPLS-AA candidate",
}
_FORCITE_PROFILES = {
    "compassiii": "prepare_compassiii_v1",
    "pcff": "prepare_pcff_v1",
    "dreiding_qeq": "prepare_dreiding_qeq_v1",
    "universal_qeq": "prepare_universal_qeq_v1",
}


def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest().upper()


def _finite_number(value: Any, label: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number")
    result = float(value)
    if not math.isfinite(result) or (minimum is not None and result < minimum):
        raise ValueError(f"{label} must be a finite number >= {minimum}")
    return result


def _validate_name(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SAFE_NAME.fullmatch(value.strip()) is None:
        raise ValueError(f"{label} must be a non-empty string of at most 120 characters")
    return value.strip()


def _strict_mapping(value: Any, allowed: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"Unknown {label} fields: {unknown}")
    return dict(value)


def normalize_model_spec(model_spec: dict[str, Any]) -> dict[str, Any]:
    """Validate a deliberately partial, but unambiguous, model specification."""

    spec = _strict_mapping(
        model_spec,
        {"label", "model_class", "components", "structure", "target", "forcefield", "conditions", "system_charge"},
        "model_spec",
    )
    result: dict[str, Any] = {}
    if "label" in spec:
        result["label"] = _validate_name(spec["label"], "model_spec.label")
    if "model_class" in spec:
        if spec["model_class"] not in _MODEL_CLASSES:
            raise ValueError(f"model_spec.model_class must be one of {sorted(_MODEL_CLASSES)}")
        result["model_class"] = spec["model_class"]

    components = spec.get("components", [])
    if not isinstance(components, list) or len(components) > 64:
        raise ValueError("model_spec.components must be an array containing at most 64 entries")
    normalized_components: list[dict[str, Any]] = []
    for index, item in enumerate(components):
        component = _strict_mapping(item, {"name", "role", "count", "formula", "formal_charge"}, f"components[{index}]")
        if "name" not in component:
            raise ValueError(f"components[{index}].name is required")
        normalized = {"name": _validate_name(component["name"], f"components[{index}].name")}
        if "role" in component:
            normalized["role"] = _validate_name(component["role"], f"components[{index}].role")
        if "formula" in component:
            normalized["formula"] = _validate_name(component["formula"], f"components[{index}].formula")
        if "count" in component:
            count = component["count"]
            if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 10_000_000:
                raise ValueError(f"components[{index}].count must be an integer from 1 to 10000000")
            normalized["count"] = count
        if "formal_charge" in component:
            normalized["formal_charge"] = _finite_number(component["formal_charge"], f"components[{index}].formal_charge", minimum=-100.0)
            if normalized["formal_charge"] > 100.0:
                raise ValueError(f"components[{index}].formal_charge must be between -100 and 100")
        normalized_components.append(normalized)
    result["components"] = normalized_components

    if "structure" in spec:
        structure = _strict_mapping(
            spec["structure"], {"path", "sha256", "source_kind", "format", "periodic_dimension"}, "model_spec.structure"
        )
        normalized_structure: dict[str, Any] = {}
        if "path" in structure:
            normalized_structure["path"] = _validate_name(structure["path"], "model_spec.structure.path")
        if "sha256" in structure:
            if not isinstance(structure["sha256"], str) or _SHA256.fullmatch(structure["sha256"]) is None:
                raise ValueError("model_spec.structure.sha256 must contain exactly 64 hexadecimal characters")
            normalized_structure["sha256"] = structure["sha256"].upper()
        if "source_kind" in structure:
            if structure["source_kind"] not in {"local_file", "derived", "database_record", "unknown"}:
                raise ValueError("model_spec.structure.source_kind is invalid")
            normalized_structure["source_kind"] = structure["source_kind"]
        if "format" in structure:
            if structure["format"] not in _STRUCTURE_FORMATS:
                raise ValueError(f"model_spec.structure.format must be one of {sorted(_STRUCTURE_FORMATS)}")
            normalized_structure["format"] = structure["format"]
        if "periodic_dimension" in structure:
            dimension = structure["periodic_dimension"]
            if isinstance(dimension, bool) or not isinstance(dimension, int) or not 0 <= dimension <= 3:
                raise ValueError("model_spec.structure.periodic_dimension must be an integer from 0 to 3")
            normalized_structure["periodic_dimension"] = dimension
        result["structure"] = normalized_structure

    if "target" in spec:
        target = _strict_mapping(spec["target"], {"engine", "purpose"}, "model_spec.target")
        normalized_target: dict[str, Any] = {}
        if "engine" in target:
            if target["engine"] not in _ENGINES:
                raise ValueError(f"model_spec.target.engine must be one of {sorted(_ENGINES)}")
            normalized_target["engine"] = target["engine"]
        if "purpose" in target:
            if target["purpose"] not in _PURPOSES:
                raise ValueError(f"model_spec.target.purpose must be one of {sorted(_PURPOSES)}")
            normalized_target["purpose"] = target["purpose"]
        result["target"] = normalized_target

    if "forcefield" in spec:
        forcefield = _strict_mapping(
            spec["forcefield"], {"name", "source_file", "source_sha256", "charge_model", "compatibility_review"},
            "model_spec.forcefield",
        )
        normalized_forcefield: dict[str, Any] = {}
        if "name" in forcefield:
            normalized_forcefield["name"] = _validate_name(forcefield["name"], "model_spec.forcefield.name")
        if "source_file" in forcefield:
            source_file = _validate_name(forcefield["source_file"], "model_spec.forcefield.source_file")
            if Path(source_file).name != source_file or Path(source_file).suffix.lower() not in _FRC_SUFFIXES:
                raise ValueError("model_spec.forcefield.source_file must be a library filename ending in .frc, .rlb, or .dat")
            normalized_forcefield["source_file"] = source_file
        if "source_sha256" in forcefield:
            if not isinstance(forcefield["source_sha256"], str) or _SHA256.fullmatch(forcefield["source_sha256"]) is None:
                raise ValueError("model_spec.forcefield.source_sha256 must contain exactly 64 hexadecimal characters")
            normalized_forcefield["source_sha256"] = forcefield["source_sha256"].upper()
        if "charge_model" in forcefield:
            if forcefield["charge_model"] not in {"provided", "qeq", "forcefield_assigned", "formal_only", "unknown"}:
                raise ValueError("model_spec.forcefield.charge_model is invalid")
            normalized_forcefield["charge_model"] = forcefield["charge_model"]
        if "compatibility_review" in forcefield:
            if forcefield["compatibility_review"] not in {"not_reviewed", "reviewed_local_evidence", "reviewed_literature", "not_required"}:
                raise ValueError("model_spec.forcefield.compatibility_review is invalid")
            normalized_forcefield["compatibility_review"] = forcefield["compatibility_review"]
        result["forcefield"] = normalized_forcefield

    if "conditions" in spec:
        conditions = _strict_mapping(spec["conditions"], {"temperature_k", "pressure_atm", "density_g_cm3"}, "model_spec.conditions")
        normalized_conditions: dict[str, float] = {}
        for key in ("temperature_k", "pressure_atm", "density_g_cm3"):
            if key in conditions:
                normalized_conditions[key] = _finite_number(conditions[key], f"model_spec.conditions.{key}", minimum=0.0)
        result["conditions"] = normalized_conditions

    if "system_charge" in spec:
        charge = _strict_mapping(spec["system_charge"], {"net_charge", "neutrality_required"}, "model_spec.system_charge")
        normalized_charge: dict[str, Any] = {}
        if "net_charge" in charge:
            normalized_charge["net_charge"] = _finite_number(charge["net_charge"], "model_spec.system_charge.net_charge", minimum=-1_000_000.0)
            if normalized_charge["net_charge"] > 1_000_000.0:
                raise ValueError("model_spec.system_charge.net_charge must be between -1000000 and 1000000")
        if "neutrality_required" in charge:
            if not isinstance(charge["neutrality_required"], bool):
                raise ValueError("model_spec.system_charge.neutrality_required must be boolean")
            normalized_charge["neutrality_required"] = charge["neutrality_required"]
        result["system_charge"] = normalized_charge
    return result


def discover_local_forcefields(forcefield_root: Path | None = None) -> list[dict[str, Any]]:
    """List bounded metadata for configured local LAMMPS force-field resources."""

    if forcefield_root is None:
        configured = load_pipeline_config()["software"]["lammps"].get("frc_files")
        forcefield_root = Path(configured) if configured else None
    if forcefield_root is None or not forcefield_root.is_dir():
        return []
    results: list[dict[str, Any]] = []
    for path in sorted(forcefield_root.iterdir(), key=lambda item: item.name.lower()):
        if not path.is_file() or path.suffix.lower() not in _FRC_SUFFIXES or path.stat().st_size > 64 * 1024 * 1024:
            continue
        results.append({
            "filename": path.name,
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
            "label": _LOCAL_FRC_LABELS.get(path.name.lower(), "Local force-field resource candidate"),
            "scientific_status": "candidate_only_requires_compatibility_review",
        })
    return results


def discover_local_structure_sources(
    search_roots: list[str] | None,
    *,
    path_resolver: Callable[[str], Path] = lambda value: resolve_workspace_path(value, must_exist=True),
) -> list[dict[str, Any]]:
    """Bound a user-directed local scan to known structure file formats only."""

    if search_roots is None:
        return []
    if not isinstance(search_roots, list) or len(search_roots) > 4:
        raise ValueError("search_roots must contain at most four workspace directories")
    candidates: list[dict[str, Any]] = []
    for root_text in search_roots:
        if not isinstance(root_text, str) or not root_text:
            raise ValueError("search_roots entries must be non-empty strings")
        root = path_resolver(root_text)
        if not root.is_dir():
            raise ValueError(f"search root is not a directory: {root}")
        visited = 0
        for path in root.rglob("*"):
            visited += 1
            if visited > 2_000 or len(candidates) >= 32:
                break
            if not path.is_file() or path.suffix.lower() not in _STRUCTURE_SUFFIXES:
                continue
            try:
                relative = path.relative_to(root).as_posix()
                size = path.stat().st_size
            except OSError:
                continue
            if size > 512 * 1024 * 1024:
                continue
            candidates.append({
                "search_root": str(root),
                "relative_path": relative,
                "format": "car_mdf" if path.suffix.lower() in {".car", ".mdf"} else path.suffix.lower().lstrip("."),
                "bytes": size,
                "selection_required": True,
            })
        if len(candidates) >= 32:
            break
    return sorted(candidates, key=lambda item: (item["search_root"].lower(), item["relative_path"].lower()))


def _check(identifier: str, status: str, message: str, *, resolution: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "id": identifier,
        "status": status,
        "message": message,
        "resolution": resolution,
        "details": details or {},
    }


def _structure_check(structure: dict[str, Any], *, path_resolver: Callable[[str], Path]) -> tuple[list[dict[str, Any]], int | None]:
    checks: list[dict[str, Any]] = []
    inferred_periodic_dimension = structure.get("periodic_dimension")
    path_text = structure.get("path")
    if not path_text:
        source_kind = structure.get("source_kind", "unknown")
        checks.append(_check(
            "structure_source", "resolvable" if source_kind == "database_record" else "blocked",
            "A readable local structure file is not specified.",
            resolution="explicit_public_metadata_search" if source_kind == "database_record" else "provide_local_structure_or_database_identifier",
        ))
        return checks, inferred_periodic_dimension
    source = path_resolver(path_text)
    if not source.is_file():
        raise ValueError("model_spec.structure.path must identify a file")
    suffix = source.suffix.lower()
    if suffix not in _STRUCTURE_SUFFIXES:
        checks.append(_check(
            "structure_format", "blocked", "The local structure format is not supported by this readiness assessor.",
            resolution="convert_or_supply_cif_xsd_car_mdf_pdb_sdf_or_mol2", details={"suffix": suffix},
        ))
    else:
        actual = sha256_file(source)
        expected = structure.get("sha256")
        if expected and expected != actual:
            checks.append(_check(
                "structure_integrity", "blocked", "The supplied structure SHA-256 does not match the local file.",
                resolution="freeze_the_intended_input_and_update_the_hash", details={"actual_sha256": actual},
            ))
        else:
            checks.append(_check(
                "structure_source", "pass", "A readable local structure source was found.",
                resolution="not_required", details={"format": "car_mdf" if suffix in {".car", ".mdf"} else suffix.lstrip("."), "sha256": actual},
            ))
        if suffix == ".xsd":
            try:
                geometry = inspect_xsd_geometry(source)
                inferred_periodic_dimension = geometry["periodic_dimension"]
                checks.append(_check(
                    "structure_geometry", "pass", "XSD geometry was inspected locally.", resolution="not_required",
                    details={key: geometry[key] for key in ("atom_count", "bond_count", "periodic_dimension", "elements")},
                ))
            except Exception as exc:
                checks.append(_check(
                    "structure_geometry", "resolvable", "The XSD could not be fully inspected by the lightweight assessor.",
                    resolution="run_md_structure_preflight", details={"reason": str(exc)},
                ))
    return checks, inferred_periodic_dimension


def assess_model_readiness(
    model_spec: dict[str, Any],
    *,
    search_roots: list[str] | None = None,
    path_resolver: Callable[[str], Path] = lambda value: resolve_workspace_path(value, must_exist=True),
    forcefield_root: Path | None = None,
) -> dict[str, Any]:
    """Return a non-executing intake assessment for a potentially incomplete model."""

    spec = normalize_model_spec(model_spec)
    checks: list[dict[str, Any]] = []
    model_class = spec.get("model_class")
    target = spec.get("target", {})
    engine = target.get("engine")
    purpose = target.get("purpose")
    components = spec["components"]
    forcefields = discover_local_forcefields(forcefield_root)
    local_sources = discover_local_structure_sources(search_roots, path_resolver=path_resolver)

    if model_class:
        checks.append(_check("model_class", "pass", "Model class is specified.", resolution="not_required", details={"model_class": model_class}))
    else:
        checks.append(_check("model_class", "blocked", "Model class is missing.", resolution="human_scientific_decision"))
    if components:
        checks.append(_check("component_inventory", "pass", "At least one model component is specified.", resolution="not_required", details={"component_count": len(components)}))
    else:
        checks.append(_check("component_inventory", "blocked", "No components are specified.", resolution="provide_components_roles_and_counts"))
    if engine and purpose:
        checks.append(_check("target", "pass", "Target engine and purpose are specified.", resolution="not_required", details={"engine": engine, "purpose": purpose}))
    else:
        checks.append(_check("target", "blocked", "Target engine and purpose must both be specified.", resolution="human_scientific_decision"))

    structure_checks, periodic_dimension = _structure_check(spec.get("structure", {}), path_resolver=path_resolver)
    checks.extend(structure_checks)
    if model_class in _PERIODIC_MODEL_CLASSES:
        if periodic_dimension == 3:
            checks.append(_check("periodic_cell", "pass", "Three-dimensional periodicity is available for this model class.", resolution="not_required"))
        elif periodic_dimension is None:
            checks.append(_check("periodic_cell", "resolvable", "Periodic cell information is not yet available.", resolution="derive_from_selected_crystal_or_surface_source"))
        else:
            checks.append(_check("periodic_cell", "blocked", "This model class requires a three-dimensionally periodic parent/cell.", resolution="supply_or_build_a_valid_periodic_parent", details={"periodic_dimension": periodic_dimension}))

    if engine in {"forcite", "lammps"}:
        forcefield = spec.get("forcefield", {})
        name = str(forcefield.get("name", "")).strip().lower()
        source_file = forcefield.get("source_file")
        source_hash = forcefield.get("source_sha256")
        available_by_name = {item["filename"].lower(): item for item in forcefields}
        if not name:
            status = "resolvable" if forcefields or engine == "forcite" else "blocked"
            checks.append(_check(
                "forcefield_selection", status, "No force field/profile is selected.",
                resolution="review_local_candidates_and_select_one" if status == "resolvable" else "provide_reviewed_forcefield",
                details={"candidate_count": len(forcefields), "forcite_profiles": sorted(_FORCITE_PROFILES)},
            ))
        elif engine == "forcite" and name in _FORCITE_PROFILES:
            checks.append(_check(
                "forcefield_selection", "pass", "A closed Forcite preparation profile is named.", resolution="run_profile_preflight_before_any_execution", details={"profile": _FORCITE_PROFILES[name]}))
        elif source_file and source_file.lower() in available_by_name:
            checks.append(_check(
                "forcefield_selection", "pass", "The selected local force-field resource exists.", resolution="run_msi2lmp_preflight_before_conversion", details={"filename": source_file, "sha256": available_by_name[source_file.lower()]["sha256"]}))
        else:
            checks.append(_check(
                "forcefield_selection", "blocked", "The named force field is not a closed Forcite profile or a discovered local library resource.",
                resolution="provide_reviewed_parameter_provenance_or_request_manual_parameterization", details={"name": name or None, "source_file": source_file},
            ))
        if source_file and source_file.lower() in available_by_name:
            actual = available_by_name[source_file.lower()]["sha256"]
            if source_hash and source_hash != actual:
                checks.append(_check("forcefield_integrity", "blocked", "The supplied force-field SHA-256 does not match the local resource.", resolution="freeze_the_intended_forcefield_and_update_the_hash", details={"actual_sha256": actual}))
            else:
                checks.append(_check("forcefield_integrity", "pass", "The selected local force-field resource was hashed.", resolution="not_required", details={"sha256": actual}))
        elif source_file:
            checks.append(_check("forcefield_integrity", "blocked", "The selected force-field file is not in the configured local library.", resolution="do_not_import_unreviewed_forcefield_files"))

        charge_model = forcefield.get("charge_model", "unknown")
        charged_components = any(abs(float(item.get("formal_charge", 0.0))) > 1e-12 for item in components)
        if charge_model == "unknown":
            checks.append(_check(
                "charge_model", "blocked" if charged_components else "resolvable",
                "No partial-charge assignment method is specified.",
                resolution="provide_validated_charge_method_or_select_a_closed_qeq_profile",
            ))
        elif charge_model == "formal_only" and charged_components:
            checks.append(_check("charge_model", "blocked", "Formal component charge is not a partial-charge model for force-field execution.", resolution="provide_validated_partial_charges"))
        else:
            checks.append(_check("charge_model", "pass", "A charge-assignment method is specified.", resolution="run_structure_preflight_to_verify_actual_atom_charges", details={"charge_model": charge_model}))
        review = forcefield.get("compatibility_review", "not_reviewed")
        if review in {"reviewed_local_evidence", "reviewed_literature", "not_required"}:
            checks.append(_check("forcefield_compatibility_review", "pass", "Compatibility review status is recorded by the caller.", resolution="retain_evidence_in_project_manifest", details={"review": review}))
        else:
            checks.append(_check("forcefield_compatibility_review", "resolvable", "Force-field compatibility has not been reviewed for this exact system.", resolution="human_scientific_review_required"))

    if engine == "castep":
        checks.append(_check(
            "castep_execution_boundary", "blocked",
            "This MCP has no public general CASTEP execution capability.",
            resolution="use_only_the_existing_fixed_profile_preflight_or_complete_a_separate_qualification",
        ))
    if purpose == "dynamics" and engine in {"forcite", "lammps"}:
        temperature = spec.get("conditions", {}).get("temperature_k")
        if temperature is None or temperature <= 0.0:
            checks.append(_check("temperature", "blocked", "Dynamics requires an explicitly selected positive temperature.", resolution="human_scientific_decision"))
        else:
            checks.append(_check("temperature", "pass", "A positive dynamics temperature is specified.", resolution="not_required", details={"temperature_k": temperature}))

    neutrality_required = spec.get("system_charge", {}).get("neutrality_required", True)
    net_charge = spec.get("system_charge", {}).get("net_charge")
    if neutrality_required and net_charge is not None and abs(net_charge) > 1e-6:
        checks.append(_check("neutrality", "resolvable", "The declared system net charge is non-zero and requires an explicit compensation strategy.", resolution="use_counterion_placement_with_reviewed_charge_ledger", details={"net_charge": net_charge}))
    elif neutrality_required and net_charge is None and engine in {"forcite", "lammps"}:
        checks.append(_check("neutrality", "resolvable", "Net charge is not yet declared; final neutrality must be checked on the typed structure.", resolution="run_md_structure_preflight"))
    else:
        checks.append(_check("neutrality", "pass", "No declared neutrality blocker was found.", resolution="run_final_structure_preflight_before_execution"))

    blocked = [item for item in checks if item["status"] == "blocked"]
    resolvable = [item for item in checks if item["status"] == "resolvable"]
    readiness = "blocked" if blocked else ("resolvable" if resolvable else "ready")
    return {
        "schema_version": 1,
        "assessment_id": _canonical_hash({"model_spec": spec, "search_roots": search_roots or []}),
        "readiness": readiness,
        "execution_allowed": False,
        "scientific_validity": "not_determined",
        "ready_definition": "All machine-discoverable intake fields are available; this is not force-field compatibility approval or permission to execute.",
        "normalized_model_spec": spec,
        "checks": checks,
        "blockers": [{"id": item["id"], "message": item["message"], "resolution": item["resolution"]} for item in blocked],
        "resolvable_gaps": [{"id": item["id"], "message": item["message"], "resolution": item["resolution"]} for item in resolvable],
        "local_forcefield_candidates": forcefields,
        "local_structure_candidates": local_sources,
        "safety_notes": [
            "Local files are discovery candidates, not scientific evidence of force-field compatibility.",
            "No structure, parameter, cross-term, or partial charge was generated or modified.",
            "Any real write, Materials Studio calculation, or LAMMPS run remains subject to the existing dry-run, hash, preflight, and confirmation controls.",
        ],
    }


def build_model_gap_resolution_plan(
    model_spec: dict[str, Any],
    *,
    search_roots: list[str] | None = None,
    path_resolver: Callable[[str], Path] = lambda value: resolve_workspace_path(value, must_exist=True),
    forcefield_root: Path | None = None,
) -> dict[str, Any]:
    """Translate a readiness assessment into explicit, non-executing next actions."""

    assessment = assess_model_readiness(
        model_spec, search_roots=search_roots, path_resolver=path_resolver, forcefield_root=forcefield_root,
    )
    actions: list[dict[str, Any]] = []
    for gap in assessment["blockers"] + assessment["resolvable_gaps"]:
        action: dict[str, Any] = {
            "gap_id": gap["id"],
            "priority": 1 if gap["id"] in {"model_class", "component_inventory", "target", "structure_source"} else 2,
            "resolution": gap["resolution"],
            "automatic": False,
            "requires_human_review": gap["resolution"] in {
                "human_scientific_decision", "human_scientific_review_required", "provide_validated_partial_charges",
                "provide_reviewed_parameter_provenance_or_request_manual_parameterization",
            },
            "public_tool": None,
        }
        if gap["id"] in {"structure_source", "structure_geometry", "periodic_cell"}:
            action.update({"public_tool": "md_structure_preflight", "automatic": gap["id"] == "structure_geometry"})
        elif gap["id"] in {"forcefield_selection", "forcefield_integrity"}:
            action.update({"public_tool": "md_msi2lmp_preflight"})
        elif gap["id"] in {"neutrality", "charge_model"}:
            action.update({"public_tool": "ms_geology_place_counterions" if gap["id"] == "neutrality" else "md_structure_preflight"})
        actions.append(action)
    if not assessment["normalized_model_spec"].get("structure", {}).get("path") and assessment["normalized_model_spec"]["components"]:
        actions.append({
            "gap_id": "public_metadata_evidence", "priority": 2,
            "resolution": "optional_public_metadata_search",
            "automatic": False,
            "requires_human_review": True,
            "public_tool": "md_search_public_model_evidence",
            "network_policy": "Requires dry_run=false, allow_network=true, and a single-use confirmation token. It returns metadata only and never downloads structures or parameters.",
        })
    actions.sort(key=lambda item: (item["priority"], item["gap_id"]))
    return {
        "schema_version": 1,
        "plan_id": _canonical_hash({"assessment_id": assessment["assessment_id"], "actions": actions}),
        "readiness": assessment["readiness"],
        "execution_allowed": False,
        "assessment_id": assessment["assessment_id"],
        "actions": actions,
        "automatic_resolution_boundary": [
            "The plan may locate existing local files and compute hashes only.",
            "It must not invent force-field parameters, partial charges, cross terms, crystal cells, or scientific conclusions.",
            "Every candidate selected from local or public sources still requires the existing format, hash, and scientific review gates.",
        ],
    }
