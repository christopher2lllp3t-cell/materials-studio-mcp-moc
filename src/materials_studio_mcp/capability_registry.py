from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .pipeline_config import PROJECT_ROOT, load_pipeline_config


REGISTRY_PATH = PROJECT_ROOT / "config" / "materialsscript-capabilities.json"
_LAYERS = {"Core", "Structure", "Forcite", "CASTEP", "Results", "Reports"}
_STATUSES = {"verified", "unverified", "unsupported", "todo"}
_EXPOSURES = {"public", "internal", "not_implemented"}
_EVIDENCE_KINDS = {"local_documentation", "regression_test", "runtime_receipt"}
_ROOTS = {"project", "workspace", "local_ms_scripting_doc", "local_ms_castep_doc"}


def _strict_keys(data: dict[str, Any], allowed: set[str], required: set[str], label: str) -> None:
    unknown = set(data) - allowed
    missing = required - set(data)
    if unknown:
        raise ValueError(f"Unknown {label} keys: {sorted(unknown)}")
    if missing:
        raise ValueError(f"Missing {label} keys: {sorted(missing)}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def _validate_registry(data: dict[str, Any]) -> None:
    _strict_keys(
        data,
        {"schema_version", "target", "policy", "evidence", "capabilities"},
        {"schema_version", "target", "policy", "evidence", "capabilities"},
        "capability registry",
    )
    if data["schema_version"] != 1:
        raise ValueError("Unsupported capability registry schema_version")
    target = data["target"]
    _strict_keys(target, {"product", "release"}, {"product", "release"}, "target")
    if target != {"product": "BIOVIA Materials Studio", "release": "2023/23.1"}:
        raise ValueError("Capability registry target must be BIOVIA Materials Studio 2023/23.1")
    policy = data["policy"]
    _strict_keys(
        policy,
        {"unregistered_is_verified", "natural_language_to_perl", "parameter_policy"},
        {"unregistered_is_verified", "natural_language_to_perl", "parameter_policy"},
        "policy",
    )
    if policy != {
        "unregistered_is_verified": False,
        "natural_language_to_perl": False,
        "parameter_policy": "closed_allowlist",
    }:
        raise ValueError("Capability registry safety policy cannot be relaxed")

    evidence_ids: set[str] = set()
    for item in data["evidence"]:
        _strict_keys(item, {"id", "kind", "root", "path", "sha256"}, {"id", "kind", "root", "path", "sha256"}, "evidence")
        if not isinstance(item["id"], str) or not item["id"] or item["id"] in evidence_ids:
            raise ValueError("Evidence ids must be non-empty and unique")
        evidence_ids.add(item["id"])
        if item["kind"] not in _EVIDENCE_KINDS or item["root"] not in _ROOTS:
            raise ValueError(f"Unsupported capability evidence source: {item['id']}")
        if not isinstance(item["path"], str) or not item["path"] or Path(item["path"]).is_absolute():
            raise ValueError(f"Capability evidence paths must be non-empty and relative: {item['id']}")
        if not isinstance(item["sha256"], str) or len(item["sha256"]) != 64:
            raise ValueError(f"Capability evidence must have SHA-256: {item['id']}")

    capability_ids: set[str] = set()
    for item in data["capabilities"]:
        _strict_keys(
            item,
            {"id", "layer", "status", "verified", "exposure", "api_symbols", "parameters", "evidence_ids", "notes"},
            {"id", "layer", "status", "verified", "exposure", "api_symbols", "parameters", "evidence_ids", "notes"},
            "capability",
        )
        capability_id = item["id"]
        if not isinstance(capability_id, str) or not capability_id or capability_id in capability_ids:
            raise ValueError("Capability ids must be non-empty and unique")
        capability_ids.add(capability_id)
        if item["layer"] not in _LAYERS or item["status"] not in _STATUSES or item["exposure"] not in _EXPOSURES:
            raise ValueError(f"Invalid capability classification: {capability_id}")
        if not isinstance(item["verified"], bool) or item["verified"] != (item["status"] == "verified"):
            raise ValueError(f"Capability verified/status mismatch: {capability_id}")
        if not isinstance(item["api_symbols"], list) or not all(isinstance(value, str) and value for value in item["api_symbols"]):
            raise ValueError(f"Capability api_symbols must be a string list: {capability_id}")
        refs = item["evidence_ids"]
        if not isinstance(refs, list) or any(ref not in evidence_ids for ref in refs):
            raise ValueError(f"Capability has unknown evidence ids: {capability_id}")
        if item["verified"] and not refs:
            raise ValueError(f"Verified capability requires evidence: {capability_id}")
        if not item["verified"] and item["exposure"] != "not_implemented":
            raise ValueError(f"Unverified capabilities must not be exposed: {capability_id}")
        if not isinstance(item["parameters"], list):
            raise ValueError(f"Capability parameters must be a list: {capability_id}")
        parameter_names: set[str] = set()
        for parameter in item["parameters"]:
            _strict_keys(parameter, {"name", "status", "verified", "evidence_ids"}, {"name", "status", "verified", "evidence_ids"}, "parameter")
            name = parameter["name"]
            if not isinstance(name, str) or not name or name in parameter_names:
                raise ValueError(f"Capability parameter names must be non-empty and unique: {capability_id}")
            parameter_names.add(name)
            if parameter["status"] not in _STATUSES or parameter["verified"] != (parameter["status"] == "verified"):
                raise ValueError(f"Capability parameter verified/status mismatch: {capability_id}.{name}")
            parameter_refs = parameter["evidence_ids"]
            if not isinstance(parameter_refs, list) or any(ref not in evidence_ids for ref in parameter_refs):
                raise ValueError(f"Capability parameter has unknown evidence: {capability_id}.{name}")
            if parameter["verified"] and not parameter_refs:
                raise ValueError(f"Verified capability parameter requires evidence: {capability_id}.{name}")


def load_capability_registry(path: str | Path | None = None) -> dict[str, Any]:
    source = Path(path).resolve() if path else REGISTRY_PATH.resolve()
    data = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Capability registry must be a JSON object")
    _validate_registry(data)
    return data


def _evidence_roots() -> dict[str, Path]:
    config = load_pipeline_config()
    workspace = Path(config["policy"]["workspace_roots"][0]).resolve()
    ms_root = Path(config["software"]["materials_studio"]["root"]).resolve()
    return {
        "project": PROJECT_ROOT.resolve(),
        "workspace": workspace,
        "local_ms_scripting_doc": ms_root / "share" / "doc" / "content" / "scripting",
        "local_ms_castep_doc": ms_root / "share" / "doc" / "content" / "modules" / "castep",
    }


def audit_capability_registry(path: str | Path | None = None) -> dict[str, Any]:
    data = load_capability_registry(path)
    roots = _evidence_roots()
    checks: list[dict[str, Any]] = []
    valid_ids: set[str] = set()
    for item in data["evidence"]:
        source = (roots[item["root"]] / Path(item["path"])).resolve()
        try:
            source.relative_to(roots[item["root"]].resolve())
            contained = True
        except ValueError:
            contained = False
        exists = contained and source.is_file()
        actual = _sha256(source) if exists else None
        matches = exists and actual == item["sha256"].upper()
        if matches:
            valid_ids.add(item["id"])
        checks.append({
            "id": item["id"],
            "kind": item["kind"],
            "path": str(source),
            "exists": exists,
            "hash_matches": matches,
        })
    capabilities: list[dict[str, Any]] = []
    for item in data["capabilities"]:
        evidence_valid = bool(item["evidence_ids"]) and all(ref in valid_ids for ref in item["evidence_ids"])
        parameters_valid = all(
            not parameter["verified"] or all(ref in valid_ids for ref in parameter["evidence_ids"])
            for parameter in item["parameters"]
        )
        effective_verified = item["verified"] and evidence_valid and parameters_valid
        capabilities.append({**item, "declared_verified": item["verified"], "verified": effective_verified})
    return {
        "schema_version": 1,
        "status": "pass" if all(check["hash_matches"] for check in checks) else "degraded",
        "target": data["target"],
        "policy": data["policy"],
        "evidence_checks": checks,
        "capabilities": capabilities,
        "summary": {
            "declared_verified": sum(1 for item in data["capabilities"] if item["verified"]),
            "effective_verified": sum(1 for item in capabilities if item["verified"]),
            "unverified_or_unsupported": sum(1 for item in capabilities if not item["verified"]),
        },
    }
