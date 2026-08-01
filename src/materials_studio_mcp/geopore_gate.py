from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

from .pipeline_config import resolve_workspace_path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def assess_geopore_contract(path: str) -> dict[str, Any]:
    """Fail-closed intake gate for paper-grade mineral nanopore construction.

    This validator deliberately does not build or repair a surface.  It releases
    construction only after every chemistry, geometry, periodicity and provenance
    decision is explicit and internally consistent.
    """
    source = resolve_workspace_path(path, must_exist=True)
    data = json.loads(source.read_text(encoding="utf-8"))
    required = {
        "schema_version", "gate_id", "literature_evidence", "source_bulk", "cleavage", "termination",
        "hydroxylation", "double_surface", "pore_width", "pbc_electrostatics",
        "fixed_regions", "fluid", "force_field",
    }
    errors: list[str] = []
    blockers: list[str] = []
    unknown = sorted(set(data) - required)
    missing = sorted(required - set(data))
    if unknown: errors.append(f"Unknown top-level fields: {unknown}")
    if missing: errors.append(f"Missing top-level fields: {missing}")
    if errors:
        return {"schema_version": 1, "validator": "geopore_gate", "status": "fail",
                "construction_released": False, "errors": errors, "blockers": blockers}

    if data.get("schema_version") != 2:
        errors.append("Geopore intake schema_version must be 2")

    literature = data["literature_evidence"]
    literature_data: dict[str, Any] = {}
    verified_literature_sources = 0
    verified_visual_reviews = 0
    literature_path_value = literature.get("path")
    literature_hash = literature.get("sha256")
    if not literature_path_value or not literature_hash:
        blockers.append("Hashed literature evidence is required")
    else:
        try:
            literature_path = resolve_workspace_path(literature_path_value, must_exist=True)
            if _sha256(literature_path) != str(literature_hash).upper():
                errors.append("Literature evidence SHA-256 mismatch")
            try:
                literature_data = json.loads(literature_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeError):
                errors.append("Literature evidence is not valid UTF-8 JSON")
            sources = literature_data.get("sources") if isinstance(literature_data, dict) else None
            if not isinstance(sources, list) or not sources:
                errors.append("Literature evidence must contain at least one source")
            else:
                for index, item in enumerate(sources):
                    if not isinstance(item, dict) or not item.get("file") or not item.get("sha256"):
                        errors.append(f"Literature source {index} requires file and sha256")
                        continue
                    try:
                        artifact = resolve_workspace_path(literature_path.parent / str(item["file"]), must_exist=True)
                        if _sha256(artifact) != str(item["sha256"]).upper():
                            errors.append(f"Literature source {index} SHA-256 mismatch")
                        else:
                            verified_literature_sources += 1
                    except (FileNotFoundError, PermissionError):
                        errors.append(f"Literature source {index} is unavailable or outside workspace")
            reviews = literature_data.get("visual_review") if isinstance(literature_data, dict) else None
            if not isinstance(reviews, list) or not reviews:
                errors.append("Literature evidence must contain visual-review artifacts")
            else:
                for index, item in enumerate(reviews):
                    if not isinstance(item, dict) or not item.get("render") or not item.get("sha256"):
                        errors.append(f"Visual review {index} requires render and sha256")
                        continue
                    try:
                        render = resolve_workspace_path(literature_path.parent / str(item["render"]), must_exist=True)
                        if _sha256(render) != str(item["sha256"]).upper():
                            errors.append(f"Visual review {index} SHA-256 mismatch")
                        elif item.get("result") != "pass":
                            errors.append(f"Visual review {index} is not marked pass")
                        else:
                            verified_visual_reviews += 1
                    except (FileNotFoundError, PermissionError):
                        errors.append(f"Visual review {index} is unavailable or outside workspace")
        except (FileNotFoundError, PermissionError):
            errors.append("Literature evidence path is unavailable or outside workspace")

    bulk = data["source_bulk"]
    local_path = bulk.get("local_path")
    expected_hash = bulk.get("sha256")
    if not local_path or not expected_hash:
        blockers.append("Hashed accepted bulk alpha-quartz source is required")
    else:
        try:
            bulk_path = resolve_workspace_path(local_path, must_exist=True)
            if _sha256(bulk_path) != str(expected_hash).upper():
                errors.append("Bulk source SHA-256 mismatch")
        except (FileNotFoundError, PermissionError):
            errors.append("Bulk source path is unavailable or outside workspace")
    evidence_path_value = bulk.get("runtime_evidence_path")
    evidence_hash = bulk.get("runtime_evidence_sha256")
    if not evidence_path_value or not evidence_hash:
        blockers.append("Accepted bulk runtime evidence hash is required")
    else:
        try:
            evidence_path = resolve_workspace_path(evidence_path_value, must_exist=True)
            if _sha256(evidence_path) != str(evidence_hash).upper():
                errors.append("Bulk runtime evidence SHA-256 mismatch")
        except (FileNotFoundError, PermissionError):
            errors.append("Bulk runtime evidence path is unavailable or outside workspace")

    cleavage = data["cleavage"]
    miller = cleavage.get("miller_index")
    if not (isinstance(miller, list) and len(miller) == 3 and all(isinstance(v, int) for v in miller) and any(miller)):
        blockers.append("A nonzero integer Miller index is required")
    for key in ("plane_origin_rule", "coordinate_transform", "slab_thickness_A", "lateral_repeat"):
        if cleavage.get(key) in (None, "", []): blockers.append(f"Cleavage {key} is required")

    termination = data["termination"]
    for side in ("top", "bottom"):
        surface = termination.get(side) or {}
        for key in ("termination_name", "selection_rule", "literature_citation"):
            if not surface.get(key): blockers.append(f"{side} surface {key} is required")
    if termination.get("dangling_bond_policy") != "no_unreviewed_automatic_repair":
        blockers.append("Dangling-bond repair must be explicit and review-gated")

    hydrox = data["hydroxylation"]
    for key in ("algorithm", "protonation_rule", "literature_citation", "top_silanol_density_OH_nm2",
                "bottom_silanol_density_OH_nm2", "surface_atom_type_map"):
        if hydrox.get(key) in (None, "", {}): blockers.append(f"Hydroxylation {key} is required")
    if hydrox.get("unresolved_undercoordinated_sites") not in (0,):
        blockers.append("All under-coordinated surface sites must be resolved and counted")
    charge = hydrox.get("net_charge_e")
    if not isinstance(charge, (int, float)) or not math.isfinite(charge) or abs(charge) > 1.0e-6:
        blockers.append("Hydroxylated double-wall model must be neutral within 1e-6 e")

    double = data["double_surface"]
    for key in ("construction_method", "second_surface_transform", "surface_equivalence_audit"):
        if double.get(key) in (None, "", {}): blockers.append(f"Double-surface {key} is required")

    width = data["pore_width"]
    geometric, accessible = width.get("geometric_A"), width.get("accessible_A")
    for key in ("reference_plane_definition", "probe_species", "probe_radius_A", "accessible_width_algorithm"):
        if width.get(key) in (None, ""): blockers.append(f"Pore-width {key} is required")
    if not isinstance(geometric, (int, float)) or not math.isfinite(geometric) or geometric <= 0:
        blockers.append("Positive geometric pore width is required")
    if not isinstance(accessible, (int, float)) or not math.isfinite(accessible) or accessible <= 0:
        blockers.append("Positive accessible pore width is required")
    elif isinstance(geometric, (int, float)) and accessible >= geometric:
        errors.append("Accessible pore width must be smaller than geometric width")

    decision = literature_data.get("contract_decision", {}) if isinstance(literature_data, dict) else {}
    supported = decision.get("fields_with_direct_support", {}) if isinstance(decision, dict) else {}
    if supported:
        if supported.get("cleavage.miller_index") != miller:
            errors.append("Contract Miller index does not match hashed literature evidence")
        supported_width = supported.get("pore_width.geometric_A")
        if (not isinstance(supported_width, (int, float)) or
                not isinstance(geometric, (int, float)) or
                not math.isclose(float(supported_width), float(geometric), rel_tol=0.0, abs_tol=1.0e-9)):
            errors.append("Contract geometric pore width does not match hashed literature evidence")
    else:
        errors.append("Literature evidence lacks contract_decision.fields_with_direct_support")

    electro = data["pbc_electrostatics"]
    mode, boundary = electro.get("mode"), electro.get("lammps_boundary")
    if mode == "3d_periodic_two_interface":
        if boundary != ["p", "p", "p"]: errors.append("3D two-interface mode requires p p p")
        if electro.get("kspace") not in {"pppm", "ewald"}: blockers.append("3D mode requires declared long-range kspace")
        if electro.get("slab_correction") not in (False, "none"): errors.append("3D mode must not claim 2D slab correction")
        if electro.get("periodic_image_pore_audit") is not True: blockers.append("3D mode requires periodic-image pore audit")
    elif mode == "2d_isolated_slab":
        if boundary != ["p", "p", "f"]: errors.append("2D isolated mode requires p p f")
        if electro.get("slab_correction") != "kspace_modify slab 3.0": blockers.append("2D mode requires explicit slab correction")
        if not isinstance(electro.get("vacuum_A"), (int, float)): blockers.append("2D mode requires explicit vacuum thickness")
    else:
        blockers.append("Electrostatics mode must be 3d_periodic_two_interface or 2d_isolated_slab")

    fixed = data["fixed_regions"]
    if fixed.get("selection_semantics") != "immutable_atom_ids":
        blockers.append("Fixed regions must use immutable atom IDs")
    if not fixed.get("top_atom_ids") or not fixed.get("bottom_atom_ids"):
        blockers.append("Top and bottom fixed-region atom-ID lists are required")

    fluid = data["fluid"]
    for key in ("composition", "molecule_counts", "temperature_K", "pressure_target"):
        if fluid.get(key) in (None, "", {}): blockers.append(f"Fluid {key} is required")

    force_field = data["force_field"]
    for key in ("mineral_name", "mineral_reference", "fluid_name", "fluid_reference", "compatibility_review", "parameter_file_sha256"):
        if force_field.get(key) in (None, "", {}): blockers.append(f"Force-field {key} is required")

    status = "fail" if errors else ("blocked" if blockers else "pass")
    return {"schema_version": 2, "validator": "materials_studio_mcp.geopore_gate",
            "status": status, "construction_released": status == "pass",
            "errors": errors, "blockers": blockers,
            "semantic_decisions": {"geometric_and_accessible_widths_distinct": True,
                                   "formal_and_partial_charge_distinct": True,
                                   "wrapped_for_density_unwrapped_for_msd": True,
                                   "verified_literature_sources": verified_literature_sources,
                                   "verified_visual_reviews": verified_visual_reviews}}
