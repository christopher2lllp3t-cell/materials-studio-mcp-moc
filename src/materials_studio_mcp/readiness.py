from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .capability_registry import audit_capability_registry
from .pipeline_config import PROJECT_ROOT, load_pipeline_config


ENVIRONMENT_PATH = PROJECT_ROOT / "config" / "research-environment.local.json"
REQUIREMENTS_PATH = PROJECT_ROOT / "config" / "research-workflow-requirements.json"


def _load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def load_research_environment(path: str | Path | None = None) -> dict[str, Any]:
    data = _load_object(Path(path).resolve() if path else ENVIRONMENT_PATH.resolve())
    if data.get("schema_version") != 1:
        raise ValueError("Unsupported research environment schema_version")
    for key in ("workspace_root", "science_root", "namd", "refprop", "castep", "ch03"):
        if key not in data:
            raise ValueError(f"Missing research environment key: {key}")
    return data


def load_workflow_requirements(path: str | Path | None = None) -> dict[str, Any]:
    data = _load_object(Path(path).resolve() if path else REQUIREMENTS_PATH.resolve())
    if data.get("schema_version") != 1 or not isinstance(data.get("workflows"), list):
        raise ValueError("Invalid research workflow requirements registry")
    policy = data.get("policy")
    if policy != {
        "missing_requirement_blocks_workflow": True,
        "unverified_capability_blocks_execution": True,
        "manual_gate_never_auto_passes": True,
    }:
        raise ValueError("Research workflow safety policy cannot be relaxed")
    return data


def _lookup(data: dict[str, Any], dotted: str) -> Any:
    value: Any = data
    for key in dotted.split("."):
        if not isinstance(value, dict) or key not in value:
            raise KeyError(dotted)
        value = value[key]
    return value


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def audit_research_readiness() -> dict[str, Any]:
    config = load_pipeline_config()
    environment = load_research_environment()
    requirements = load_workflow_requirements()
    bindings = {"software": config["software"], "policy": config["policy"], "environment": environment}
    capability_audit = audit_capability_registry()
    capabilities = {item["id"]: item for item in capability_audit["capabilities"]}
    workflows: list[dict[str, Any]] = []
    for workflow in requirements["workflows"]:
        checks: list[dict[str, Any]] = []
        for requirement in workflow["requirements"]:
            kind = requirement["kind"]
            check: dict[str, Any] = {"id": requirement["id"], "kind": kind, "status": "pass"}
            if kind == "path":
                value = Path(str(_lookup(bindings, requirement["binding"]))).expanduser()
                exists = value.is_file() if requirement["path_type"] == "file" else value.is_dir()
                check.update({"path": str(value), "exists": exists, "status": "pass" if exists else "blocked", "code": None if exists else "PATH_MISSING"})
            elif kind == "required_file_in_directory":
                root = Path(str(_lookup(bindings, requirement["binding"]))).expanduser()
                value = root / requirement["relative_path"]
                exists = value.is_file()
                check.update({"path": str(value), "exists": exists, "status": "pass" if exists else "blocked", "code": None if exists else "REQUIRED_INPUT_MISSING"})
            elif kind == "capability":
                capability = capabilities.get(requirement["capability_id"])
                verified = bool(capability and capability.get("verified"))
                check.update({"capability_id": requirement["capability_id"], "verified": verified, "status": "pass" if verified else "blocked", "code": None if verified else "CAPABILITY_UNVERIFIED"})
            elif kind == "hashed_evidence":
                value = Path(str(_lookup(bindings, requirement["binding"]))).expanduser()
                expected = str(_lookup(bindings, requirement["sha256_binding"])).upper()
                exists = value.is_file()
                actual = _hash(value) if exists else None
                matches = exists and actual == expected
                check.update({"path": str(value), "exists": exists, "hash_matches": matches, "status": "pass" if matches else "blocked", "code": None if matches else "EVIDENCE_HASH_MISMATCH"})
            elif kind == "manual_gate":
                check.update({"status": "blocked", "code": requirement["code"], "gate_status": requirement["status"]})
            else:
                raise ValueError(f"Unsupported readiness requirement kind: {kind}")
            checks.append(check)
        blocked = [check for check in checks if check["status"] != "pass"]
        workflows.append({
            "id": workflow["id"],
            "label": workflow["label"],
            "status": "ready" if not blocked else "blocked",
            "execution_allowed": not blocked,
            "checks": checks,
            "blockers": [{"code": check.get("code"), "requirement": check["id"]} for check in blocked],
        })
    return {
        "schema_version": 1,
        "status": "pass" if capability_audit["status"] == "pass" else "degraded",
        "configuration": {"software": config["paths"]["software"], "policy": config["paths"]["policy"], "environment": str(ENVIRONMENT_PATH), "requirements": str(REQUIREMENTS_PATH)},
        "capability_registry": capability_audit["summary"],
        "workflows": workflows,
        "execution_allowed_workflows": [item["id"] for item in workflows if item["execution_allowed"]],
        "blocked_workflows": [item["id"] for item in workflows if not item["execution_allowed"]],
        "next_action": "Complete manual gates and obtain verified local API evidence before enabling blocked workflows.",
    }
