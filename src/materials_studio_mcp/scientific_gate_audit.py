from __future__ import annotations

"""Fail-closed target-model science intake and evidence audit.

This module only reads the target structure and already-recorded evidence. It
never runs Materials Studio, LAMMPS, VMD, or any other scientific engine.
"""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from .confirmation import confirmation_manager
from .project_manager import register_artifact
from .structure_preflight import inspect_structure_preflight
from .pipeline_config import load_pipeline_config, resolve_workspace_path


_ROOT = Path(__file__).resolve().parents[2]
_ALLOWED_STATUSES = {"PASS", "BLOCKED", "UNVERIFIED", "NOT_APPLICABLE"}
_REQUIRED_FIELDS = (
    "source_evidence", "structure_file", "cell", "surface", "charge_compensation",
    "fixed_regions", "fluid", "forcefield", "atom_types_and_charges", "electrostatics",
    "lammps_units", "replicas", "trajectory_plan",
)
_GATE_IDS = (
    "intake_completeness", "literature_model_consistency", "coordinate_box_consistency",
    "atom_element_consistency", "surface_bilateral_geometry_chemistry", "charge_audit",
    "forcefield_coverage", "ms_lammps_energy_force_consistency", "run0", "short_minimization",
    "short_dynamics", "equilibration_production_separation", "replicate_seed_plan",
    "wrapped_unwrapped_trajectory_semantics",
)
_UNRESOLVED = {"", "UNKNOWN", "UNRESOLVED", "TODO", "TBD", "NOT_SET"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().lower()


def _hash_json(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


def _write_json_exclusive(path: Path, value: Any) -> None:
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing == value:
            return
        raise FileExistsError(f"Refusing to overwrite scientific audit artifact: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def _gate(gate_id: str, status: str, *, observations: list[str] | None = None, evidence_ids: list[str] | None = None, blockers: list[str] | None = None, next_actions: list[str] | None = None) -> dict[str, Any]:
    if status not in _ALLOWED_STATUSES:
        raise ValueError(f"Unsupported scientific gate status: {status}")
    return {"gate_id": gate_id, "status": status, "observations": observations or [], "evidence_ids": evidence_ids or [], "blockers": blockers or [], "next_actions": next_actions or []}


def _missing_intake(contract: dict[str, Any]) -> list[str]:
    missing = [field for field in _REQUIRED_FIELDS if field not in contract]
    if contract.get("model_role") != "target":
        missing.append("model_role=target")
    source = contract.get("source_evidence")
    if isinstance(source, dict):
        for key in ("path", "sha256", "citation_or_id"):
            if not source.get(key):
                missing.append(f"source_evidence.{key}")
    structure = contract.get("structure_file")
    if isinstance(structure, dict):
        for key in ("path", "sha256"):
            if not structure.get(key):
                missing.append(f"structure_file.{key}")
    cell = contract.get("cell")
    if isinstance(cell, dict):
        matrix = cell.get("matrix_angstrom")
        if not isinstance(matrix, list) or len(matrix) != 3 or any(not isinstance(row, list) or len(row) != 3 for row in matrix):
            missing.append("cell.matrix_angstrom[3][3]")
        boundary = cell.get("periodic_boundary")
        if not isinstance(boundary, list) or len(boundary) != 3:
            missing.append("cell.periodic_boundary[3]")
    surface = contract.get("surface")
    if isinstance(surface, dict):
        miller = surface.get("miller_index")
        if not isinstance(miller, list) or len(miller) != 3 or not any(isinstance(item, int) and item != 0 for item in miller):
            missing.append("surface.miller_index")
    replicas = contract.get("replicas")
    if isinstance(replicas, dict):
        seeds = replicas.get("seeds")
        if not isinstance(seeds, list) or replicas.get("repeat_count") != len(seeds):
            missing.append("replicas.seeds_repeat_count_match")
    trajectory = contract.get("trajectory_plan")
    if isinstance(trajectory, dict) and (not isinstance(trajectory.get("equilibration_steps"), int) or not isinstance(trajectory.get("production_steps"), int) or trajectory.get("equilibration_steps", 0) <= 0 or trajectory.get("production_steps", 0) <= 0):
        missing.append("trajectory_plan.positive_lengths")
    surface = contract.get("surface")
    if isinstance(surface, dict):
        for key in ("termination", "hydroxylation_rule"):
            if str(surface.get(key, "")).strip().upper() in _UNRESOLVED:
                missing.append(f"surface.{key}")
    for key in ("charge_compensation", "lammps_units"):
        if str(contract.get(key, "")).strip().upper() in _UNRESOLVED:
            missing.append(key)
    fixed = contract.get("fixed_regions")
    if not isinstance(fixed, dict) or not fixed:
        missing.append("fixed_regions.explicit_definition")
    fluid = contract.get("fluid")
    if isinstance(fluid, dict) and fluid.get("not_applicable") is not True:
        if not isinstance(fluid.get("composition"), list) or not fluid.get("composition"):
            missing.append("fluid.composition")
        for key in ("density_g_cm3", "temperature_k", "pressure_mpa"):
            if not isinstance(fluid.get(key), (int, float)):
                missing.append(f"fluid.{key}")
    forcefield = contract.get("forcefield")
    if isinstance(forcefield, dict):
        for key in ("name", "version"):
            if str(forcefield.get(key, "")).strip().upper() in _UNRESOLVED:
                missing.append(f"forcefield.{key}")
        if not isinstance(forcefield.get("parameter_sources"), list) or not forcefield.get("parameter_sources"):
            missing.append("forcefield.parameter_sources")
    typing = contract.get("atom_types_and_charges")
    if isinstance(typing, dict):
        for key in ("atom_type_source", "partial_charge_source"):
            if str(typing.get(key, "")).strip().upper() in _UNRESOLVED:
                missing.append(f"atom_types_and_charges.{key}")
    electrostatics = contract.get("electrostatics")
    if isinstance(electrostatics, dict):
        for key in ("long_range", "mixing_rule", "special_bonds"):
            if str(electrostatics.get(key, "")).strip().upper() in _UNRESOLVED:
                missing.append(f"electrostatics.{key}")
    for label, item in (("source_evidence", source), ("structure_file", structure)):
        if isinstance(item, dict) and item.get("sha256") and (not isinstance(item.get("sha256"), str) or len(item["sha256"]) != 64):
            missing.append(f"{label}.sha256")
    return sorted(set(missing))


def _evidence_status(evidence: dict[str, Any], key: str, *, reject_non_target: bool = True) -> tuple[str, list[str], list[str]]:
    item = evidence.get(key)
    if not isinstance(item, dict):
        return "UNVERIFIED", [f"No evidence receipt supplied for {key}"], [f"Supply a target-model evidence receipt for {key}"]
    role = item.get("model_role")
    if reject_non_target and role in {"calibration", "qualification"}:
        return "BLOCKED", [f"Evidence for {key} is marked {role}, not target"], ["Generate target-model evidence; calibration evidence cannot be reused"]
    status = str(item.get("status", "UNVERIFIED")).upper()
    if status not in _ALLOWED_STATUSES:
        status = "UNVERIFIED"
    if status == "PASS" and role != "target":
        return "UNVERIFIED", [f"Evidence for {key} does not explicitly declare model_role=target"], ["Bind the evidence receipt to the exact target model"]
    if status == "PASS" and not item.get("evidence_ids"):
        return "UNVERIFIED", [f"Evidence for {key} claims PASS without evidence_ids"], ["Register and hash the target-model evidence receipts"]
    return status, list(item.get("observations", [])), list(item.get("next_actions", []))


def _resolve_existing(value: Any, config: dict[str, Any]) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return Path(resolve_workspace_path(value, config=config, must_exist=True)).resolve()
    except (FileNotFoundError, PermissionError, ValueError, OSError):
        return None


def _audit_contract(project_directory: str, contract: dict[str, Any], evidence: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    config = load_pipeline_config()
    project = Path(resolve_workspace_path(project_directory, config=config, must_exist=True)).resolve()
    if not (project / "manifest.json").is_file():
        raise FileNotFoundError(f"Materials Studio MCP project manifest is missing: {project / 'manifest.json'}")
    missing = _missing_intake(contract)
    gates: list[dict[str, Any]] = []
    if missing:
        gates.append(_gate("intake_completeness", "BLOCKED", blockers=[f"Missing frozen intake fields: {', '.join(missing)}"], next_actions=["Provide all 14 target-model intake groups before freezing the contract"]))
    else:
        gates.append(_gate("intake_completeness", "PASS", observations=["All 14 required intake groups are present and model_role=target"]))
    source = contract.get("source_evidence", {})
    structure_spec = contract.get("structure_file", {})
    structure_path = _resolve_existing(structure_spec.get("path"), config)
    structure_digest = _sha256(structure_path) if structure_path and structure_path.is_file() else None
    source_path = _resolve_existing(source.get("path"), config)
    source_digest = _sha256(source_path) if source_path and source_path.is_file() else None
    if source_digest != str(source.get("sha256", "")).lower():
        gates[0]["status"] = "BLOCKED"
        gates[0]["blockers"].append("Source literature/raw-structure evidence is missing or its SHA-256 does not match")
        gates[0]["next_actions"].append("Copy the authoritative source evidence into the project and freeze its SHA-256")
    if structure_path is None or structure_digest != str(structure_spec.get("sha256", "")).lower():
        gates.append(_gate("coordinate_box_consistency", "BLOCKED", blockers=["Target structure is missing or SHA-256 does not match the frozen intake"], next_actions=["Provide the exact target structure and recompute SHA-256"]))
        gates.append(_gate("atom_element_consistency", "BLOCKED", blockers=["Target structure cannot be audited until its hash is verified"]))
    else:
        audit = inspect_structure_preflight(str(structure_path))
        expected_cell = contract["cell"]["matrix_angstrom"]
        actual_cell = audit.get("cell", {}).get("vectors") if isinstance(audit.get("cell"), dict) else None
        cell_match = bool(actual_cell and all(abs(float(actual_cell[i][j]) - float(expected_cell[i][j])) <= 1e-6 for i in range(3) for j in range(3)))
        gates.append(_gate("coordinate_box_consistency", "PASS" if cell_match else "BLOCKED", observations=[f"Structure hash verified: {structure_digest}", f"Periodic boundary contract: {contract['cell']['periodic_boundary']}", f"Cell matrix match: {cell_match}"], blockers=[] if cell_match else ["Coordinate extent and frozen periodic cell do not match"], next_actions=[] if cell_match else ["Resolve coordinate-range versus periodic-box ambiguity"]))
        expected_elements = contract.get("expected_elements")
        atom_observations = [f"Observed atom_count={audit.get('atom_count')}", f"Observed elements={audit.get('elements')}", f"Observed bond_count={audit.get('bond_count')}", f"Observed bond_types={audit.get('bond_types')}"]
        atom_status = "PASS" if audit.get("status") == "pass" and (expected_elements is None or audit.get("elements") == expected_elements) else "UNVERIFIED"
        gates.append(_gate("atom_element_consistency", atom_status, observations=atom_observations, next_actions=[] if atom_status == "PASS" else ["Add frozen target atom/element expectations and resolve any mismatch"]))
    literature_status, literature_obs, literature_next = _evidence_status(evidence, "literature_model_comparison")
    gates.append(_gate("literature_model_consistency", literature_status, observations=literature_obs, evidence_ids=list(evidence.get("literature_model_comparison", {}).get("evidence_ids", [])) if isinstance(evidence.get("literature_model_comparison"), dict) else [], next_actions=literature_next))
    for gate_id, evidence_key in (("surface_bilateral_geometry_chemistry", "surface_bilateral_audit"), ("charge_audit", "charge_audit"), ("forcefield_coverage", "forcefield_coverage"), ("ms_lammps_energy_force_consistency", "energy_force_equivalence"), ("run0", "lammps_run0"), ("short_minimization", "short_minimization"), ("short_dynamics", "short_dynamics"), ("wrapped_unwrapped_trajectory_semantics", "trajectory_semantics")):
        status, observations, next_actions = _evidence_status(evidence, evidence_key)
        item = evidence.get(evidence_key)
        gates.append(_gate(gate_id, status, observations=observations, evidence_ids=list(item.get("evidence_ids", [])) if isinstance(item, dict) else [], blockers=[] if status != "BLOCKED" else [f"Target-model evidence is blocked or uses a non-target receipt: {evidence_key}"], next_actions=next_actions))
    forcefield_gate = next(item for item in gates if item["gate_id"] == "forcefield_coverage")
    parameter_errors: list[str] = []
    for parameter in contract.get("forcefield", {}).get("parameter_sources", []):
        path = _resolve_existing(parameter.get("path") if isinstance(parameter, dict) else None, config)
        if path is None or _sha256(path) != str(parameter.get("sha256", "")).lower():
            parameter_errors.append(str(parameter.get("path") if isinstance(parameter, dict) else parameter))
    if parameter_errors:
        forcefield_gate["status"] = "BLOCKED"
        forcefield_gate["blockers"].append("Forcefield parameter source is missing or hash-mismatched: " + ", ".join(parameter_errors))
        forcefield_gate["next_actions"].append("Provide every target forcefield parameter file with a verified SHA-256")
    plan = contract.get("trajectory_plan", {})
    repeats = contract.get("replicas", {})
    separation = plan.get("equilibration_steps", 0) > 0 and plan.get("production_steps", 0) > 0
    gates.append(_gate("equilibration_production_separation", "PASS" if separation else "BLOCKED", observations=[f"equilibration_steps={plan.get('equilibration_steps')}", f"production_steps={plan.get('production_steps')}"], blockers=[] if separation else ["Equilibration and production lengths are not both positive"], next_actions=[] if separation else ["Freeze separate equilibration and production lengths"]))
    seed_status = "PASS" if isinstance(repeats.get("seeds"), list) and len(repeats.get("seeds", [])) == repeats.get("repeat_count") else "BLOCKED"
    gates.append(_gate("replicate_seed_plan", seed_status, observations=[f"repeat_count={repeats.get('repeat_count')}", f"seeds={repeats.get('seeds')}"], blockers=[] if seed_status == "PASS" else ["Repeat count and random-seed list are inconsistent"], next_actions=[] if seed_status == "PASS" else ["Freeze one explicit seed per planned repeat"]))
    blockers = [blocker for gate in gates for blocker in gate["blockers"]]
    unverified = [gate["gate_id"] for gate in gates if gate["status"] == "UNVERIFIED"]
    report = {"schema_version": 1, "audit_type": "target_model_scientific_gate_audit", "created_at": _now(), "project_directory": str(project), "model_id": contract.get("model_id"), "model_role": contract.get("model_role"), "qualification_only": True, "release_decision": "BLOCKED" if blockers or unverified else "REVIEW_REQUIRED", "gates": gates, "blocker_count": len(blockers), "unverified_gate_ids": unverified, "input_contract_sha256": _hash_json(contract), "evidence_manifest_sha256": _hash_json(evidence), "interpretation": "Platform test counts are not scientific evidence. Calibration or qualification evidence is not target-model evidence. No production release state is emitted by this audit."}
    blocker_register = {"schema_version": 1, "created_at": report["created_at"], "model_id": contract.get("model_id"), "blockers": [{"blocker_id": f"SCI-{index:03d}", "gate_id": gate["gate_id"], "reason": reason, "status": "BLOCKED"} for index, (gate, reason) in enumerate(((gate, blocker) for gate in gates for blocker in gate["blockers"]), start=1)]}
    return report, {"blocker_register": blocker_register, "project": project, "structure_digest": structure_digest, "source_evidence": source}


def _markdown_report(report: dict[str, Any]) -> str:
    lines = ["# Scientific Gate Report", "", f"- Model: `{report.get('model_id')}`", f"- Role: `{report.get('model_role')}`", f"- Decision: **{report.get('release_decision')}**", "- Scope: qualification audit only", "", "| Gate | Status | Observations | Blockers |", "|---|---|---|---|"]
    for gate in report["gates"]:
        lines.append(f"| `{gate['gate_id']}` | **{gate['status']}** | {'; '.join(gate['observations']) or '-'} | {'; '.join(gate['blockers']) or '-'} |")
    lines.extend(["", "## Interpretation", "", report["interpretation"], "", "Production claims are intentionally not emitted by this audit."])
    return "\n".join(lines) + "\n"


def audit_target_model_science(*, project_directory: str, target_model_contract: dict[str, Any], evidence_manifest: dict[str, Any] | None = None, dry_run: bool = True, confirmation_token: str | None = None) -> dict[str, Any]:
    if not isinstance(dry_run, bool):
        raise ValueError("dry_run must be a boolean")
    if not isinstance(target_model_contract, dict):
        raise ValueError("target_model_contract must be an object")
    evidence = evidence_manifest if isinstance(evidence_manifest, dict) else {}
    report, context = _audit_contract(project_directory, target_model_contract, evidence)
    project = context["project"]
    report_path = project / "reports" / "scientific_gate_report.json"
    markdown_path = project / "reports" / "scientific_gate_report.md"
    blocker_path = project / "reports" / "blocker_register.json"
    action_path = project / "reports" / "next_action_plan.md"
    intake_path = project / "request" / "scientific_intake_contract.json"
    result = {"status": "dry_run" if dry_run else report["release_decision"], "qualification_only": True, "release_decision": report["release_decision"], "gates": report["gates"], "blockers": [item["reason"] for item in context["blocker_register"]["blockers"]], "unverified_gate_ids": report["unverified_gate_ids"], "planned_outputs": [str(intake_path), str(report_path), str(markdown_path), str(blocker_path), str(action_path)], "input_contract_sha256": report["input_contract_sha256"], "next_actions": ["Complete blocked or unverified target-model evidence before any release decision"]}
    if dry_run:
        return result
    parameters = {"project_directory": str(project), "target_model_contract": target_model_contract, "evidence_manifest": evidence}
    if not confirmation_token:
        raise PermissionError("A confirmation_token is required to freeze the scientific intake and write reports")
    confirmation_manager.consume(confirmation_token, "md_scientific_gate_audit", parameters)
    _write_json_exclusive(intake_path, target_model_contract)
    _write_json_exclusive(report_path, report)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    if markdown_path.exists() and markdown_path.read_text(encoding="utf-8") != _markdown_report(report):
        raise FileExistsError(f"Refusing to overwrite scientific audit artifact: {markdown_path}")
    markdown_path.write_text(_markdown_report(report), encoding="utf-8", newline="\n")
    _write_json_exclusive(blocker_path, context["blocker_register"])
    action_text = "# Next Action Plan\n\n" + "\n".join(f"{index}. Resolve `{gate['gate_id']}`: " + ("; ".join(gate["next_actions"]) or "retain the recorded evidence and request independent review") for index, gate in enumerate(report["gates"], start=1)) + "\n"
    if action_path.exists() and action_path.read_text(encoding="utf-8") != action_text:
        raise FileExistsError(f"Refusing to overwrite scientific audit artifact: {action_path}")
    action_path.write_text(action_text, encoding="utf-8", newline="\n")
    for path, role in ((intake_path, "scientific_intake_contract"), (report_path, "scientific_gate_report_json"), (markdown_path, "scientific_gate_report_markdown"), (blocker_path, "blocker_register"), (action_path, "next_action_plan")):
        register_artifact(str(project), str(path), role, "md_scientific_gate_audit")
    return {**result, "status": report["release_decision"], "written": True, "report_path": str(report_path), "markdown_path": str(markdown_path), "blocker_register_path": str(blocker_path), "next_action_plan_path": str(action_path), "artifact_ids": [], "evidence_ids": []}


__all__ = ["audit_target_model_science"]
