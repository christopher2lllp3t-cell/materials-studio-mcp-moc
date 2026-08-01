from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import json
import re
import secrets
from pathlib import Path
from typing import Any, Callable

from .api_contract import canonical_hash
from .project_manager import _manifest_lock, _manifest_path, _write_manifest
from .security import redact_sensitive


_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record_path(project_directory: str, idempotency_key: str) -> Path:
    if not isinstance(idempotency_key, str) or _IDEMPOTENCY_KEY.fullmatch(idempotency_key) is None:
        raise ValueError("idempotency_key must match [A-Za-z0-9][A-Za-z0-9._-]{0,127}")
    project = _manifest_path(project_directory).parent
    root = project / ".operations"
    root.mkdir(exist_ok=True)
    root_stat = root.lstat()
    if root.is_symlink() or bool(getattr(root_stat, "st_file_attributes", 0) & 0x400):
        raise PermissionError("The project operation directory cannot be a link or junction")
    resolved_root = root.resolve()
    resolved_project = project.resolve()
    if resolved_root != resolved_project and resolved_project not in resolved_root.parents:
        raise PermissionError("The project operation directory escapes the project")
    record = root / f"{idempotency_key}.json"
    if record.exists():
        record_stat = record.lstat()
        if record.is_symlink() or bool(getattr(record_stat, "st_file_attributes", 0) & 0x400):
            raise PermissionError("Operation records cannot be links or junctions")
    return record


def _read_record(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError(f"Invalid operation record: {path}")
    return data


def begin_operation(project_directory: str, idempotency_key: str, tool: str,
                    parameters: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(tool, str) or not tool:
        raise ValueError("tool must be a non-empty string")
    if not isinstance(parameters, dict):
        raise ValueError("parameters must be an object")
    request_hash = canonical_hash({"tool": tool, "parameters": parameters})
    path = _record_path(project_directory, idempotency_key)
    with _manifest_lock(path):
        if path.exists():
            existing = _read_record(path)
            if existing.get("tool") != tool or existing.get("request_hash") != request_hash:
                raise ValueError("idempotency_key is already bound to a different operation")
            return {"created": False, "replayed": True, "record": existing, "record_path": str(path)}
        operation_token = secrets.token_urlsafe(32)
        record = {
            "schema_version": 1,
            "idempotency_key": idempotency_key,
            "operation_id": secrets.token_hex(16),
            "tool": tool,
            "request_hash": request_hash,
            "state": "running",
            "created_at": _now(),
            "updated_at": _now(),
            "owner_token_sha256": hashlib.sha256(operation_token.encode("utf-8")).hexdigest(),
            "result": None,
            "error": None,
        }
        _write_manifest(path, record)
    return {
        "created": True,
        "replayed": False,
        "operation_token": operation_token,
        "record": record,
        "record_path": str(path),
    }


def finish_operation(project_directory: str, idempotency_key: str, operation_token: str,
                     *, result: Any = None, error: dict[str, Any] | None = None) -> dict[str, Any]:
    path = _record_path(project_directory, idempotency_key)
    with _manifest_lock(path):
        if not path.exists():
            raise FileNotFoundError(f"Operation record not found: {path}")
        record = _read_record(path)
        supplied = hashlib.sha256(operation_token.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(supplied, str(record.get("owner_token_sha256", ""))):
            raise PermissionError("Invalid operation ownership token")
        if record.get("state") != "running":
            raise ValueError(f"Operation is already terminal: {record.get('state')}")
        if error is None:
            # canonical_hash also proves the stored result is finite JSON.
            canonical_hash(result)
            record["state"] = "succeeded"
            record["result"] = redact_sensitive(result)
        else:
            if not isinstance(error, dict):
                raise ValueError("error must be an object")
            canonical_hash(error)
            record["state"] = "failed"
            record["error"] = redact_sensitive(error)
        record["updated_at"] = _now()
        record.pop("owner_token_sha256", None)
        _write_manifest(path, record)
    return record


def run_idempotent(project_directory: str, idempotency_key: str | None, tool: str,
                   parameters: dict[str, Any], implementation: Callable[[], Any]) -> tuple[Any, bool]:
    """Run a synchronous project mutation once and durably replay its result."""
    if idempotency_key is None:
        return implementation(), False
    begun = begin_operation(project_directory, idempotency_key, tool, parameters)
    record = begun["record"]
    if not begun["created"]:
        if record["state"] == "succeeded":
            return record["result"], True
        if record["state"] == "failed":
            raise RuntimeError("The idempotent operation previously failed; use a new key after review")
        raise RuntimeError("The idempotent operation is already running")
    token = begun["operation_token"]
    try:
        result = implementation()
    except Exception as exc:
        finish_operation(
            project_directory, idempotency_key, token,
            error={"type": type(exc).__name__, "message": str(exc)},
        )
        raise
    finish_operation(project_directory, idempotency_key, token, result=result)
    return result, False
