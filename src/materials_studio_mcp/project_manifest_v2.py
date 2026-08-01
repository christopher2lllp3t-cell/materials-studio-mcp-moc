from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import re
from typing import Any


STATES = ("DRAFT", "STRUCTURE_VERIFIED", "FORCEFIELD_VERIFIED", "CONVERSION_VERIFIED", "LAMMPS_PREFLIGHT_VERIFIED", "QUALIFICATION_ONLY", "PRODUCTION_READY", "BLOCKED")
ARTIFACT_TYPES = ("source_structure", "derived_structure", "forcefield_bundle", "conversion_artifact", "simulation_run", "analysis_result", "evidence_receipt")
ARTIFACT_STATUSES = ("REGISTERED", "CANDIDATE", "VERIFIED", "QUALIFICATION_ONLY", "PRODUCTION_APPROVED", "BLOCKED", "INVALID")
TRANSITIONS = {
    "DRAFT": frozenset({"STRUCTURE_VERIFIED", "BLOCKED"}),
    "STRUCTURE_VERIFIED": frozenset({"FORCEFIELD_VERIFIED", "BLOCKED"}),
    "FORCEFIELD_VERIFIED": frozenset({"CONVERSION_VERIFIED", "BLOCKED"}),
    "CONVERSION_VERIFIED": frozenset({"LAMMPS_PREFLIGHT_VERIFIED", "BLOCKED"}),
    "LAMMPS_PREFLIGHT_VERIFIED": frozenset({"QUALIFICATION_ONLY", "PRODUCTION_READY", "BLOCKED"}),
    "QUALIFICATION_ONLY": frozenset({"PRODUCTION_READY", "BLOCKED"}),
    "PRODUCTION_READY": frozenset(),
    "BLOCKED": frozenset(),
}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_REQUIRED_ARTIFACT_KEYS = {"artifact_id", "artifact_type", "path", "sha256", "parent_artifact_ids", "created_by", "tool_version", "created_at", "status"}
_EVIDENCE_KINDS = {"SOFTWARE_FUNCTION", "INTERFACE_CONVERSION", "MODEL_GEOMETRY", "SCIENTIFIC_PRODUCTION"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _timestamp(value: Any, label: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be an ISO-8601 timestamp")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO-8601 timestamp") from exc


def _artifact_index(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(item["artifact_id"]): item for item in manifest.get("artifacts", [])}


def _validate_history_gate(manifest: dict[str, Any], target_state: str, evidence_ids: list[str]) -> None:
    """Reject manifests that bypass transition functions and forge a gate state."""
    if target_state == "BLOCKED":
        return
    index = _artifact_index(manifest)
    receipts = [index[item] for item in evidence_ids if item in index and index[item]["artifact_type"] == "evidence_receipt"]
    if len(receipts) != len(evidence_ids):
        raise ValueError("state history evidence must reference evidence_receipt artifacts")

    def require(kind: str, gate_name: str, statuses: set[str] | None = None) -> None:
        accepted = statuses or {"VERIFIED", "PRODUCTION_APPROVED"}
        if not any(item["status"] in accepted and item["metadata"].get("evidence_kind") == kind and item["metadata"].get("gate") == gate_name and item["metadata"].get("result") == "PASS" for item in receipts):
            raise ValueError(f"state history is missing verified {gate_name} evidence")

    if target_state == "STRUCTURE_VERIFIED":
        require("MODEL_GEOMETRY", target_state)
    elif target_state == "FORCEFIELD_VERIFIED":
        if not any(item["artifact_type"] == "forcefield_bundle" and item["status"] == "VERIFIED" for item in manifest["artifacts"]):
            raise ValueError("FORCEFIELD_VERIFIED requires a VERIFIED forcefield bundle")
        require("MODEL_GEOMETRY", target_state)
    elif target_state == "CONVERSION_VERIFIED":
        require("INTERFACE_CONVERSION", target_state)
    elif target_state == "LAMMPS_PREFLIGHT_VERIFIED":
        require("SOFTWARE_FUNCTION", target_state)
    elif target_state == "QUALIFICATION_ONLY":
        require("SOFTWARE_FUNCTION", target_state, {"VERIFIED", "QUALIFICATION_ONLY", "PRODUCTION_APPROVED"})


def _validate_artifact(artifact: Any, known_ids: set[str]) -> None:
    if not isinstance(artifact, dict) or not _REQUIRED_ARTIFACT_KEYS.issubset(artifact):
        raise ValueError("artifact is missing one or more required fields")
    if set(artifact) - (_REQUIRED_ARTIFACT_KEYS | {"metadata"}):
        raise ValueError("artifact contains unsupported fields")
    artifact_id = artifact["artifact_id"]
    if not isinstance(artifact_id, str) or _ID.fullmatch(artifact_id) is None:
        raise ValueError("artifact_id is invalid")
    if artifact["artifact_type"] not in ARTIFACT_TYPES:
        raise ValueError("artifact_type is invalid")
    path = artifact["path"]
    if not isinstance(path, str) or not path or re.match(r"^(?:[A-Za-z]:[\\/]|[\\/])", path):
        raise ValueError("artifact path must be a non-empty relative path")
    if not isinstance(artifact["sha256"], str) or _SHA256.fullmatch(artifact["sha256"]) is None:
        raise ValueError("artifact sha256 must be 64 lowercase hexadecimal characters")
    parents = artifact["parent_artifact_ids"]
    if not isinstance(parents, list) or len(set(parents)) != len(parents) or any(not isinstance(item, str) or not item for item in parents):
        raise ValueError("parent_artifact_ids must be a unique array of non-empty strings")
    if artifact["artifact_type"] == "source_structure" and parents:
        raise ValueError("source_structure cannot have parent artifacts")
    if artifact["artifact_type"] != "source_structure" and not parents:
        raise ValueError(f"{artifact['artifact_type']} must declare at least one parent artifact")
    missing = set(parents) - known_ids
    if missing:
        raise ValueError(f"parent artifact IDs are not registered: {sorted(missing)}")
    if artifact_id in parents:
        raise ValueError("artifact cannot be its own parent")
    for field in ("created_by", "tool_version"):
        if not isinstance(artifact[field], str) or not artifact[field].strip():
            raise ValueError(f"artifact {field} must be non-empty")
    _timestamp(artifact["created_at"], "artifact.created_at")
    if artifact["status"] not in ARTIFACT_STATUSES:
        raise ValueError("artifact status is invalid")
    metadata = artifact.get("metadata", {})
    if not isinstance(metadata, dict):
        raise ValueError("artifact metadata must be an object")
    if artifact["artifact_type"] == "forcefield_bundle":
        for key in ("forcefield_profile", "profile_sha256", "parameter_file_sha256", "atom_typing_coverage", "charge_audit"):
            if key not in metadata:
                raise ValueError(f"forcefield bundle metadata is missing {key}")
        if _SHA256.fullmatch(str(metadata["profile_sha256"])) is None:
            raise ValueError("forcefield profile_sha256 is invalid")
        params = metadata["parameter_file_sha256"]
        if not isinstance(params, list) or not params or any(not isinstance(item, str) or _SHA256.fullmatch(item) is None for item in params):
            raise ValueError("forcefield parameter_file_sha256 must contain at least one valid hash")
        if metadata["atom_typing_coverage"] != 1:
            raise ValueError("forcefield atom typing coverage must be exactly 1.0")
        audit = metadata["charge_audit"]
        if not isinstance(audit, dict) or audit.get("status") != "VERIFIED" or audit.get("partial_charge_coverage") != 1:
            raise ValueError("forcefield charge audit is missing or not VERIFIED")
        if not isinstance(audit.get("net_charge_e"), (int, float)) or not isinstance(audit.get("expected_net_charge_e"), (int, float)):
            raise ValueError("forcefield charge audit must include numeric net and expected charge")
        tolerance = audit.get("tolerance_e", 1e-6)
        if not isinstance(tolerance, (int, float)) or abs(audit["net_charge_e"] - audit["expected_net_charge_e"]) > tolerance:
            raise ValueError("forcefield charge audit net charge is outside tolerance")
    if artifact["artifact_type"] == "evidence_receipt":
        required = ("evidence_kind", "evidence_scope", "model_role", "gate", "result", "subject_artifact_ids")
        if any(key not in metadata for key in required):
            raise ValueError("evidence receipt metadata is incomplete")
        if metadata["evidence_kind"] not in _EVIDENCE_KINDS or metadata["model_role"] not in {"target", "calibration", "qualification"}:
            raise ValueError("evidence receipt kind or model_role is invalid")
        if metadata["result"] not in {"PASS", "FAIL", "BLOCKED", "CANDIDATE"}:
            raise ValueError("evidence receipt result is invalid")
        subjects = metadata["subject_artifact_ids"]
        if not isinstance(subjects, list) or not subjects or not set(subjects).issubset(set(parents)):
            raise ValueError("evidence receipt subjects must be registered parent artifacts")


def validate_manifest_v2(manifest: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 2:
        raise ValueError("manifest schema_version must be 2")
    if set(manifest) - {"schema_version", "project", "artifacts", "state_history", "blockers"}:
        raise ValueError("manifest contains unsupported top-level fields")
    project = manifest.get("project")
    if not isinstance(project, dict):
        raise ValueError("manifest.project must be an object")
    required_project = {"project_id", "title", "state", "model_role", "target_artifact_id", "created_by", "created_at", "updated_at"}
    if not required_project.issubset(project):
        raise ValueError("manifest.project is incomplete")
    if not isinstance(project["project_id"], str) or _ID.fullmatch(project["project_id"]) is None or not project["title"]:
        raise ValueError("project identity is invalid")
    if project["state"] not in STATES or project["model_role"] not in {"target", "calibration", "qualification"}:
        raise ValueError("project state or model_role is invalid")
    _timestamp(project["created_at"], "project.created_at")
    _timestamp(project["updated_at"], "project.updated_at")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise ValueError("manifest.artifacts must be an array")
    ids = [item.get("artifact_id") for item in artifacts if isinstance(item, dict)]
    if len(ids) != len(set(ids)):
        raise ValueError("artifact_id values must be unique")
    known_ids = set(ids)
    for artifact in artifacts:
        _validate_artifact(artifact, known_ids)
    target_id = project["target_artifact_id"]
    if target_id is not None and target_id not in known_ids:
        raise ValueError("project.target_artifact_id is not registered")
    blockers = manifest.get("blockers")
    if not isinstance(blockers, list) or any(not isinstance(item, str) or not item.strip() for item in blockers):
        raise ValueError("manifest.blockers must be an array of non-empty strings")
    history = manifest.get("state_history")
    if not isinstance(history, list):
        raise ValueError("manifest.state_history must be an array")
    current = "DRAFT"
    for record in history:
        if not isinstance(record, dict) or not {"from_state", "to_state", "evidence_ids", "authorized_by", "manual_authorization", "created_at"}.issubset(record):
            raise ValueError("state history record is incomplete")
        if record["from_state"] != current or record["to_state"] not in TRANSITIONS[current]:
            raise ValueError(f"illegal state history transition: {current} -> {record.get('to_state')}")
        if not isinstance(record["evidence_ids"], list) or not set(record["evidence_ids"]).issubset(known_ids):
            raise ValueError("state transition references missing evidence artifact")
        _validate_history_gate(manifest, record["to_state"], record["evidence_ids"])
        _timestamp(record["created_at"], "state_history.created_at")
        current = record["to_state"]
    if project["state"] != current:
        raise ValueError("project.state does not match state_history")
    if current == "BLOCKED" and not blockers:
        raise ValueError("BLOCKED projects must declare blockers")
    if current == "PRODUCTION_READY":
        if project["model_role"] != "target" or target_id is None or blockers:
            raise ValueError("PRODUCTION_READY requires an unblocked target model")
        last = history[-1]
        if not last["manual_authorization"] or not last["authorized_by"].strip():
            raise ValueError("PRODUCTION_READY requires explicit manual authorization")
        _check_production_evidence(manifest, last["evidence_ids"])
    return {"status": "valid", "state": project["state"], "artifact_count": len(artifacts), "blockers": list(blockers)}


def _check_production_evidence(manifest: dict[str, Any], evidence_ids: list[str]) -> None:
    index = _artifact_index(manifest)
    ff = [item for item in manifest["artifacts"] if item["artifact_type"] == "forcefield_bundle" and item["status"] == "VERIFIED"]
    if not ff:
        raise ValueError("PRODUCTION_READY requires a VERIFIED forcefield bundle")
    receipts = [index[item] for item in evidence_ids if item in index and index[item]["artifact_type"] == "evidence_receipt"]
    valid = [item for item in receipts if item["status"] == "PRODUCTION_APPROVED" and item["metadata"].get("evidence_kind") == "SCIENTIFIC_PRODUCTION" and item["metadata"].get("evidence_scope") == "target_model" and item["metadata"].get("model_role") == "target" and item["metadata"].get("result") == "PASS"]
    if not valid:
        raise ValueError("PRODUCTION_READY requires target-model scientific production evidence")
    target_id = manifest["project"]["target_artifact_id"]
    if not any(target_id in item["metadata"].get("subject_artifact_ids", []) for item in valid):
        raise ValueError("production evidence does not cover the target artifact")


def new_manifest_v2(project_id: str, title: str, *, model_role: str = "target", created_by: str = "materials_studio_mcp") -> dict[str, Any]:
    now = _now()
    manifest = {"schema_version": 2, "project": {"project_id": project_id, "title": title, "state": "DRAFT", "model_role": model_role, "target_artifact_id": None, "created_by": created_by, "created_at": now, "updated_at": now}, "artifacts": [], "state_history": [], "blockers": []}
    validate_manifest_v2(manifest)
    return manifest


def register_artifact_v2(manifest: dict[str, Any], artifact: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(manifest)
    if any(item.get("artifact_id") == artifact.get("artifact_id") for item in result.get("artifacts", [])):
        raise ValueError("artifact_id is already registered")
    result.setdefault("artifacts", []).append(deepcopy(artifact))
    validate_manifest_v2(result)
    return result


def set_target_artifact_v2(manifest: dict[str, Any], artifact_id: str) -> dict[str, Any]:
    result = deepcopy(manifest)
    result["project"]["target_artifact_id"] = artifact_id
    validate_manifest_v2(result)
    return result


def transition_project_state_v2(manifest: dict[str, Any], target_state: str, *, evidence_ids: list[str] | None = None, authorized_by: str = "", manual_authorization: bool = False, blockers: list[str] | None = None) -> dict[str, Any]:
    result = deepcopy(manifest)
    validate_manifest_v2(result)
    current = result["project"]["state"]
    if target_state not in STATES or target_state not in TRANSITIONS[current]:
        raise ValueError(f"illegal state transition: {current} -> {target_state}")
    ids = list(dict.fromkeys(evidence_ids or []))
    index = _artifact_index(result)
    if any(item not in index or index[item]["artifact_type"] != "evidence_receipt" for item in ids):
        raise ValueError("state transition evidence_ids must reference evidence_receipt artifacts")
    receipts = [index[item] for item in ids]
    def gate(kind: str, gate_name: str, allow_qualification: bool = False) -> None:
        if not any(item["status"] in ({"VERIFIED", "PRODUCTION_APPROVED", "QUALIFICATION_ONLY"} if allow_qualification else {"VERIFIED", "PRODUCTION_APPROVED"}) and item["metadata"].get("evidence_kind") == kind and item["metadata"].get("gate") == gate_name and item["metadata"].get("result") == "PASS" for item in receipts):
            raise PermissionError(f"missing verified {gate_name} evidence")
    if target_state == "STRUCTURE_VERIFIED": gate("MODEL_GEOMETRY", target_state)
    elif target_state == "FORCEFIELD_VERIFIED":
        if not any(item["artifact_type"] == "forcefield_bundle" and item["status"] == "VERIFIED" for item in result["artifacts"]):
            raise PermissionError("FORCEFIELD_VERIFIED requires a VERIFIED forcefield bundle")
        gate("MODEL_GEOMETRY", target_state)
    elif target_state == "CONVERSION_VERIFIED": gate("INTERFACE_CONVERSION", target_state)
    elif target_state == "LAMMPS_PREFLIGHT_VERIFIED": gate("SOFTWARE_FUNCTION", target_state)
    elif target_state == "QUALIFICATION_ONLY": gate("SOFTWARE_FUNCTION", target_state, allow_qualification=True)
    elif target_state == "PRODUCTION_READY":
        if not manual_authorization or not authorized_by.strip():
            raise PermissionError("PRODUCTION_READY requires explicit manual authorization")
        if result["project"]["model_role"] != "target" or result["project"]["target_artifact_id"] is None:
            raise PermissionError("PRODUCTION_READY requires a target model artifact")
        if result["blockers"]:
            raise PermissionError("PRODUCTION_READY is blocked")
        _check_production_evidence(result, ids)
    elif target_state == "BLOCKED":
        values = [str(item).strip() for item in (blockers or []) if str(item).strip()]
        if not values:
            raise ValueError("BLOCKED transition requires at least one blocker")
        result["blockers"] = values
    record = {"from_state": current, "to_state": target_state, "evidence_ids": ids, "authorized_by": authorized_by.strip() or "system", "manual_authorization": bool(manual_authorization), "created_at": _now()}
    result["project"]["state"] = target_state
    result["project"]["updated_at"] = record["created_at"]
    result["state_history"].append(record)
    validate_manifest_v2(result)
    return result


__all__ = ["ARTIFACT_STATUSES", "ARTIFACT_TYPES", "STATES", "TRANSITIONS", "new_manifest_v2", "register_artifact_v2", "set_target_artifact_v2", "transition_project_state_v2", "validate_manifest_v2"]
