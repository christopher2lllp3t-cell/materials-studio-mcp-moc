from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
import uuid
from typing import Any, Callable

from .public_registry import REQUEST_SCHEMA_VERSION, RESULT_SCHEMA_VERSION, public_tool_names
from .security import redact_sensitive


_OUTPUT_SLOT = re.compile(r"^[a-z0-9][a-z0-9_/-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RISKS = {"R0", "R1", "R2", "R3"}


class RecordedExecutionError(RuntimeError):
    """Execution failed after durable, structured evidence was preserved."""

    def __init__(self, message: str, data: dict[str, Any]) -> None:
        super().__init__(message)
        self.data = data


def canonical_hash(value: Any) -> str:
    try:
        payload = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("Contract values must be finite JSON values") from exc
    return hashlib.sha256(payload).hexdigest()


def validate_operation_contract(contract: dict[str, Any]) -> dict[str, Any]:
    """Validate the executable subset of the published v1 contract schema."""
    if not isinstance(contract, dict):
        raise ValueError("operation contract must be an object")
    required = {"schema_version", "tool", "risk", "project_directory", "inputs", "output_slot", "budget"}
    allowed = required | {"profile", "overrides", "gate_evidence_ids", "confirmation_token", "random_seed"}
    unknown = set(contract) - allowed
    missing = required - set(contract)
    if unknown:
        raise ValueError(f"Unknown operation contract keys: {sorted(unknown)}")
    if missing:
        raise ValueError(f"Missing operation contract keys: {sorted(missing)}")
    if contract["schema_version"] != REQUEST_SCHEMA_VERSION:
        raise ValueError(f"Unsupported operation schema_version: {contract['schema_version']!r}")
    if contract["tool"] not in public_tool_names():
        raise ValueError(f"Tool is not in the reviewed public registry: {contract['tool']!r}")
    if contract["risk"] not in _RISKS:
        raise ValueError("risk must be one of R0, R1, R2, R3")
    if not isinstance(contract["project_directory"], str) or not contract["project_directory"]:
        raise ValueError("project_directory must be a non-empty string")
    if not isinstance(contract["inputs"], list):
        raise ValueError("inputs must be an array")
    for item in contract["inputs"]:
        if not isinstance(item, dict) or set(item) != {"role", "path", "sha256"}:
            raise ValueError("each input must contain exactly role, path, and sha256")
        if not all(isinstance(item[key], str) and item[key] for key in ("role", "path", "sha256")):
            raise ValueError("input role, path, and sha256 must be non-empty strings")
        if _SHA256.fullmatch(item["sha256"]) is None:
            raise ValueError("input sha256 must be 64 lowercase hexadecimal characters")
    if not isinstance(contract["output_slot"], str) or _OUTPUT_SLOT.fullmatch(contract["output_slot"]) is None:
        raise ValueError("output_slot is invalid")
    budget = contract["budget"]
    if not isinstance(budget, dict):
        raise ValueError("budget must be an object")
    required_budget = {"wall_seconds", "max_output_bytes"}
    allowed_budget = required_budget | {"max_atoms", "max_frames", "max_steps", "mpi_processes"}
    if set(budget) - allowed_budget or required_budget - set(budget):
        raise ValueError("budget contains unknown keys or is missing required keys")
    for key, value in budget.items():
        minimum = 0 if key == "max_steps" else 1
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ValueError(f"budget.{key} must be an integer >= {minimum}")
    if budget["wall_seconds"] > 604800:
        raise ValueError("budget.wall_seconds exceeds 604800")
    if budget.get("mpi_processes", 1) > 16:
        raise ValueError("budget.mpi_processes exceeds 16")
    if contract["risk"] in {"R2", "R3"}:
        if not contract.get("confirmation_token") or not isinstance(contract.get("gate_evidence_ids"), list):
            raise ValueError("R2/R3 contracts require confirmation_token and gate_evidence_ids")
    if contract["risk"] == "R3" and not {"mpi_processes", "max_steps"}.issubset(budget):
        raise ValueError("R3 budget requires mpi_processes and max_steps")
    return dict(contract)


def success_result(tool: str, data: Any, *, operation_id: str | None = None,
                   replayed: bool = False, warnings: list[str] | None = None) -> dict[str, Any]:
    return ensure_public_result_shape({
        "schema_version": RESULT_SCHEMA_VERSION,
        "ok": True,
        "tool": tool,
        "operation_id": operation_id or uuid.uuid4().hex,
        "state": "succeeded",
        "status": "succeeded",
        "replayed": replayed,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "data": redact_sensitive(data),
        "warnings": list(warnings or []),
        "error": None,
    }, tool=tool)


def error_result(tool: str, exc: Exception, *, operation_id: str | None = None) -> dict[str, Any]:
    if isinstance(exc, PermissionError):
        code, category, retryable = "permission_denied", "authorization", False
    elif isinstance(exc, FileNotFoundError):
        code, category, retryable = "not_found", "input", False
    elif isinstance(exc, (ValueError, TypeError)):
        code, category, retryable = "invalid_request", "input", False
    elif isinstance(exc, TimeoutError):
        code, category, retryable = "timeout", "execution", True
    elif isinstance(exc, RuntimeError):
        code, category, retryable = "runtime_error", "execution", True
    else:
        code, category, retryable = "internal_error", "internal", False
    return ensure_public_result_shape({
        "schema_version": RESULT_SCHEMA_VERSION,
        "ok": False,
        "tool": tool,
        "operation_id": operation_id or uuid.uuid4().hex,
        "state": "failed",
        "status": "failed",
        "replayed": False,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "data": redact_sensitive(getattr(exc, "data", None)),
        "warnings": [],
        "error": {
            "code": code,
            "category": category,
            "message": redact_sensitive(str(exc)),
            "retryable": retryable,
        },
        "blockers": [redact_sensitive(str(exc))],
    }, tool=tool)


def ensure_public_result_shape(value: Any, *, tool: str | None = None) -> dict[str, Any]:
    """Normalize every MCP tool response without changing its scientific meaning."""
    if not isinstance(value, dict):
        value = {
            "schema_version": RESULT_SCHEMA_VERSION,
            "ok": True,
            "tool": tool or "unknown",
            "operation_id": uuid.uuid4().hex,
            "state": "succeeded",
            "status": "succeeded",
            "replayed": False,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "data": redact_sensitive(value),
            "warnings": [],
            "error": None,
        }
    result = dict(value)
    if tool and not result.get("tool"):
        result["tool"] = tool
    if not isinstance(result.get("status"), str) or not result["status"]:
        result["status"] = str(result.get("state") or ("succeeded" if result.get("ok", True) else "failed"))
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    for key in ("artifact_ids", "evidence_ids", "blockers"):
        candidate = result.get(key, data.get(key, []))
        result[key] = list(candidate) if isinstance(candidate, list) else []
    actions = result.get("next_actions", data.get("next_actions", []))
    if not isinstance(actions, list):
        singular = result.get("next_action", data.get("next_action"))
        actions = [singular] if isinstance(singular, str) and singular else []
    result["next_actions"] = list(actions)
    return redact_sensitive(result)


def invoke_with_contract(contract: dict[str, Any], implementation: Callable[[], Any], *,
                         operation_id: str | None = None) -> dict[str, Any]:
    """Single boundary for validated operation requests and versioned results."""
    tool = str(contract.get("tool", "unknown")) if isinstance(contract, dict) else "unknown"
    try:
        validated = validate_operation_contract(contract)
        return success_result(validated["tool"], implementation(), operation_id=operation_id)
    except Exception as exc:
        return error_result(tool, exc, operation_id=operation_id)
