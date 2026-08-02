from __future__ import annotations

"""Private, offline qualification parser for standalone CASTEP text output.

It reads existing files only.  It never invokes Materials Studio, RunCASTEP,
a Gateway, or any executable; it is not a public MCP capability.
"""

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

from .castep_standalone import STANDALONE_INPUT_SCHEMA_VERSION, _canonical_sha256
from .geology_modeling import sha256_file


PARSER_SCHEMA_VERSION = 1
PARSER_REVISION = "ms-mcp.private-castep-result-parser.1.3.0-p1-r1"
_SHA256 = re.compile(r"^[0-9A-Fa-f]{64}$")
_FINAL_ENERGY = re.compile(
    r"^\s*Final\s+energy(?:\s*,\s*E)?\s*=\s*(?P<value>[^\s]+)\s*eV\b", re.IGNORECASE
)
_TOTAL_TIME = re.compile(r"^\s*Total\s+time\s*=\s*(?P<value>[^\s]+)\s*s\b", re.IGNORECASE)
_NONFINITE = re.compile(r"(?<![A-Za-z0-9_])[+-]?(?:nan|inf(?:inity)?)(?![A-Za-z0-9_])", re.IGNORECASE)
_SUCCESS = re.compile(r"\b(?:geometry\s+optimization|calculation)\s+completed\s+successfully\b", re.IGNORECASE)


def _issue(code: str, detail: str) -> dict[str, str]:
    return {"code": code, "detail": detail}


def _empty_result(expected_sha256: str | None, exit_code: int | None, termination: str | None) -> dict[str, Any]:
    return {
        "schema_version": PARSER_SCHEMA_VERSION,
        "parser_revision": PARSER_REVISION,
        "status": "failed",
        "classification": "input_integrity_failure",
        "completion_evidence": [],
        "energy": None,
        "warnings": [],
        "errors": [],
        "blockers": [
            _issue("CASTEP_EXECUTION_UNVERIFIED", "Offline parsing does not qualify CASTEP execution."),
            _issue("CASTEP_RESULT_PARSING_NOT_RELEASED", "This private qualification layer is not a public scientific result parser."),
        ],
        "input_hashes": {
            "manifest_sha256": None,
            "source_sha256": None,
            "input_source_copy_sha256": None,
            "cell_sha256": None,
            "param_sha256": None,
            "contract_file_sha256": None,
            "contract_canonical_sha256": None,
        },
        "output_hashes": {"expected_sha256": expected_sha256, "observed_sha256": None},
        "seedname": None,
        "process": {"exit_code": exit_code, "termination": termination},
    }


def _normalise_inputs(
    expected_output_sha256: str | None, process_exit_code: int | None, termination: str | None
) -> tuple[str | None, int | None, str | None]:
    if expected_output_sha256 is not None:
        if not isinstance(expected_output_sha256, str) or _SHA256.fullmatch(expected_output_sha256) is None:
            raise ValueError("expected_output_sha256 must contain exactly 64 hexadecimal characters")
        expected_output_sha256 = expected_output_sha256.upper()
    if process_exit_code is not None and (isinstance(process_exit_code, bool) or not isinstance(process_exit_code, int)):
        raise ValueError("process_exit_code must be an integer or None")
    if termination is not None:
        if not isinstance(termination, str):
            raise ValueError("termination must be 'timeout', 'cancelled', or None")
        termination = {"cancel": "cancelled", "canceled": "cancelled"}.get(termination.strip().lower(), termination.strip().lower())
        if termination not in {"timeout", "cancelled"}:
            raise ValueError("termination must be 'timeout', 'cancelled', or None")
    return expected_output_sha256, process_exit_code, termination


def _declared_path(manifest_path: Path, value: Any) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        path = manifest_path.parent / path
    return path.resolve()


def _put_hash(result: dict[str, Any], key: str, value: str) -> None:
    result["input_hashes"][key] = value.upper()


def _input_contract(manifest_path: Path, result: dict[str, Any]) -> tuple[str | None, list[dict[str, str]]]:
    """Validate the complete input binding before output text is interpreted."""

    errors: list[dict[str, str]] = []
    if not manifest_path.is_file():
        return None, [_issue("MANIFEST_MISSING", "The standalone input manifest is not a regular file.")]
    try:
        manifest_bytes = manifest_path.read_bytes()
        _put_hash(result, "manifest_sha256", hashlib.sha256(manifest_bytes).hexdigest())
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, [_issue("MANIFEST_UNREADABLE", f"The standalone input manifest is unreadable: {type(exc).__name__}.")]
    if not isinstance(manifest, dict):
        return None, [_issue("MANIFEST_INVALID", "The standalone input manifest must be a JSON object.")]

    seedname = manifest.get("seedname")
    if not isinstance(seedname, str) or re.fullmatch(r"[A-Za-z0-9_]+", seedname) is None:
        errors.append(_issue("MANIFEST_SEED_INVALID", "The standalone input manifest has no safe seedname."))
        seedname = None
    if manifest.get("status") != "prepared" or manifest.get("writes_performed") is not True:
        errors.append(_issue("MANIFEST_NOT_PREPARED", "The manifest is not a written standalone input candidate."))
    if manifest.get("execution_allowed") is not False:
        errors.append(_issue("EXECUTION_POLICY_INVALID", "The input contract must remain execution-blocked."))

    artifacts = (
        ("input_source_copy", "input_source_copy_sha256", "input_source.xsd"),
        ("cell", "cell_sha256", f"{seedname}.cell" if seedname else None),
        ("param", "param_sha256", f"{seedname}.param" if seedname else None),
        ("contract", "contract_file_sha256", "standalone_input_contract.json"),
    )
    actual_hashes: dict[str, str] = {}
    contract_path: Path | None = None
    for field, hash_key, expected_name in artifacts:
        item = manifest.get(field)
        if not isinstance(item, dict):
            errors.append(_issue("MANIFEST_ARTIFACT_MISSING", f"Manifest artifact '{field}' is missing."))
            continue
        expected_hash = item.get("sha256")
        path = _declared_path(manifest_path, item.get("path"))
        if not isinstance(expected_hash, str) or _SHA256.fullmatch(expected_hash) is None:
            errors.append(_issue("MANIFEST_ARTIFACT_HASH_INVALID", f"Manifest artifact '{field}' has no valid SHA-256."))
            continue
        if path is None or not path.is_file():
            errors.append(_issue("INPUT_ARTIFACT_MISSING", f"Input artifact '{field}' is not a regular file."))
            continue
        if path.parent != manifest_path.parent.resolve():
            errors.append(_issue("INPUT_ARTIFACT_OUTSIDE_PACKAGE", f"Input artifact '{field}' is outside its manifest directory."))
            continue
        if expected_name is not None and path.name != expected_name:
            errors.append(_issue("INPUT_ARTIFACT_NAME_MISMATCH", f"Input artifact '{field}' does not match the manifest seed."))
            continue
        actual_hash = sha256_file(path)
        _put_hash(result, hash_key, actual_hash)
        actual_hashes[field] = actual_hash
        if actual_hash != expected_hash.upper():
            errors.append(_issue("INPUT_ARTIFACT_HASH_MISMATCH", f"Input artifact '{field}' does not match its manifest SHA-256."))
        if field == "contract":
            contract_path = path

    source = manifest.get("source")
    source_hash = source.get("sha256") if isinstance(source, dict) else None
    if not isinstance(source_hash, str) or _SHA256.fullmatch(source_hash) is None:
        errors.append(_issue("MANIFEST_SOURCE_HASH_INVALID", "Manifest source has no valid SHA-256."))
    else:
        _put_hash(result, "source_sha256", source_hash)
        if actual_hashes.get("input_source_copy") != source_hash.upper():
            errors.append(_issue("SOURCE_COPY_HASH_MISMATCH", "Copied XSD is not bound to the manifest source hash."))

    if contract_path is None:
        return seedname, errors
    try:
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        errors.append(_issue("CONTRACT_UNREADABLE", f"The standalone input contract is unreadable: {type(exc).__name__}."))
        return seedname, errors
    if not isinstance(contract, dict):
        errors.append(_issue("CONTRACT_INVALID", "The standalone input contract must be a JSON object."))
        return seedname, errors
    try:
        canonical_hash = _canonical_sha256(contract)
    except ValueError:
        errors.append(_issue("CONTRACT_NONFINITE", "The standalone input contract has non-finite JSON values."))
        return seedname, errors
    _put_hash(result, "contract_canonical_sha256", canonical_hash)
    declared_canonical = (manifest.get("contract") or {}).get("canonical_sha256")
    if canonical_hash != str(declared_canonical).upper() or canonical_hash != str(manifest.get("contract_sha256")).upper():
        errors.append(_issue("CONTRACT_CANONICAL_HASH_MISMATCH", "Contract canonical SHA-256 differs from its manifest."))
    contract_source = contract.get("source")
    if (
        contract.get("schema_version") != STANDALONE_INPUT_SCHEMA_VERSION
        or contract.get("tool") != "ms_prepare_castep_standalone_inputs"
        or contract.get("execution_allowed") is not False
        or not isinstance(contract_source, dict)
        or str(contract_source.get("sha256", "")).upper() != str(source_hash or "").upper()
        or not isinstance(contract.get("settings"), dict)
    ):
        errors.append(_issue("CONTRACT_BINDING_INVALID", "The contract does not match the standalone input manifest."))
    return seedname, errors


def _failure_markers(lines: list[str]) -> dict[str, list[int]]:
    """Collect diagnostics without returning raw output lines to callers."""

    markers = {name: [] for name in ("license_unavailable", "scf_not_converged", "fatal_error", "timeout", "cancelled")}
    for number, line in enumerate(lines, start=1):
        lower = line.lower()
        if ("license" in lower or "licence" in lower) and any(
            phrase in lower
            for phrase in ("unavailable", "not available", "cannot obtain", "unable to obtain", "denied", "checkout failed", "failed")
        ):
            markers["license_unavailable"].append(number)
        if ("scf" in lower or "electronic minim" in lower) and any(
            phrase in lower for phrase in ("not converg", "did not converg", "failed")
        ):
            markers["scf_not_converged"].append(number)
        if (
            "fatal" in lower
            or "segmentation fault" in lower
            or "internal error" in lower
            or "aborted" in lower
            or re.match(r"\s*error\s*[:!]", line, re.IGNORECASE) is not None
        ):
            markers["fatal_error"].append(number)
        if (
            "timed out" in lower
            or "timeout" in lower
            or ("walltime" in lower and any(word in lower for word in ("exceeded", "reached", "limit")))
            or ("time limit" in lower and any(word in lower for word in ("exceeded", "reached")))
        ):
            markers["timeout"].append(number)
        if any(phrase in lower for phrase in ("cancelled", "canceled", "terminated by user", "terminated by scheduler")):
            markers["cancelled"].append(number)
    return markers


def _completion_evidence(lines: list[str]) -> tuple[list[dict[str, Any]], float | None, bool]:
    evidence: list[dict[str, Any]] = []
    energy: float | None = None
    total_time: float | None = None
    nonfinite = False
    for number, line in enumerate(lines, start=1):
        energy_match = _FINAL_ENERGY.match(line)
        if energy_match:
            try:
                value = float(energy_match.group("value"))
            except ValueError:
                nonfinite = True
            else:
                if math.isfinite(value):
                    energy = value
                    evidence.append({"kind": "final_energy", "line_number": number, "value_eV": value})
                else:
                    nonfinite = True
        time_match = _TOTAL_TIME.match(line)
        if time_match:
            try:
                value = float(time_match.group("value"))
            except ValueError:
                nonfinite = True
            else:
                if math.isfinite(value) and value >= 0.0:
                    total_time = value
                    evidence.append({"kind": "total_time", "line_number": number, "seconds": value})
                else:
                    nonfinite = True
        if _SUCCESS.search(line):
            evidence.append({"kind": "explicit_success_marker", "line_number": number})
        if _NONFINITE.search(line):
            nonfinite = True
    return evidence, energy if energy is not None and total_time is not None else None, nonfinite


def parse_standalone_castep_result(
    *,
    castep_output: Path,
    input_manifest: Path,
    expected_output_sha256: str | None = None,
    process_exit_code: int | None = None,
    termination: str | None = None,
) -> dict[str, Any]:
    """Fail closed while qualifying one existing standalone ``.castep`` text file.

    ``completed`` requires a valid, execution-blocked input contract, exact
    seed/file-name binding, finite final energy plus total time, and no failure
    marker.  The operating-system exit code is advisory only.
    """

    expected_sha256, process_exit_code, termination = _normalise_inputs(
        expected_output_sha256, process_exit_code, termination
    )
    output_path = Path(castep_output).resolve()
    manifest_path = Path(input_manifest).resolve()
    result = _empty_result(expected_sha256, process_exit_code, termination)

    seedname, input_errors = _input_contract(manifest_path, result)
    result["seedname"] = seedname
    if input_errors:
        result["errors"] = input_errors
        return result

    output_errors: list[dict[str, str]] = []
    if not output_path.is_file():
        output_bytes = None
        output_errors.append(_issue("OUTPUT_MISSING", "The CASTEP output is not a regular file."))
    else:
        try:
            output_bytes = output_path.read_bytes()
        except OSError as exc:
            output_bytes = None
            output_errors.append(_issue("OUTPUT_UNREADABLE", f"The CASTEP output is unreadable: {type(exc).__name__}."))
    if output_bytes is not None:
        observed_sha256 = hashlib.sha256(output_bytes).hexdigest().upper()
        result["output_hashes"]["observed_sha256"] = observed_sha256
        if expected_sha256 is not None and observed_sha256 != expected_sha256:
            output_errors.append(_issue("OUTPUT_SHA256_MISMATCH", "CASTEP output does not match the expected SHA-256."))
        try:
            output_text = output_bytes.decode("utf-8")
        except UnicodeDecodeError:
            output_text = None
            output_errors.append(_issue("OUTPUT_ENCODING_INVALID", "CASTEP output is not valid UTF-8 text."))
        if sha256_file(output_path) != observed_sha256:
            output_errors.append(_issue("OUTPUT_CHANGED_DURING_PARSE", "CASTEP output changed while being parsed."))
    else:
        output_text = None
    if output_path.name != f"{seedname}.castep":
        result["classification"] = "seed_contract_mismatch"
        result["errors"] = [_issue("OUTPUT_SEED_MISMATCH", "CASTEP output file name does not match the input manifest seed."), *output_errors]
        return result
    if output_errors:
        result["classification"] = "output_integrity_failure"
        result["errors"] = output_errors
        return result
    assert output_text is not None

    lines = output_text.splitlines()
    evidence, energy, nonfinite = _completion_evidence(lines)
    result["completion_evidence"] = evidence
    markers = _failure_markers(lines)
    active_markers = {name: numbers for name, numbers in markers.items() if numbers}
    completion_proven = energy is not None and any(item["kind"] == "total_time" for item in evidence)
    if process_exit_code not in (None, 0):
        result["warnings"].append(_issue("NONZERO_EXIT_CODE", "A non-zero exit code did not decide parser status."))

    if nonfinite:
        result["classification"] = "nonfinite_numeric"
        result["errors"] = [_issue("NONFINITE_NUMERIC", "Output contains NaN or Infinity, or a non-finite required value.")]
    elif completion_proven and (active_markers or termination is not None):
        result["classification"] = "conflicting_markers"
        result["errors"] = [_issue("CONFLICTING_COMPLETION_MARKERS", "Completion evidence conflicts with failure or termination evidence.")]
    elif termination == "timeout" or markers["timeout"]:
        result["classification"] = "timeout"
        result["errors"] = [_issue("TIMEOUT", "Timeout evidence prevents completion qualification.")]
    elif termination == "cancelled" or markers["cancelled"]:
        result["classification"] = "cancelled"
        result["errors"] = [_issue("CANCELLED", "Cancellation evidence prevents completion qualification.")]
    elif markers["license_unavailable"]:
        result["classification"] = "license_unavailable"
        result["errors"] = [_issue("LICENSE_UNAVAILABLE", "License-unavailable evidence prevents completion qualification.")]
    elif markers["scf_not_converged"]:
        result["classification"] = "scf_not_converged"
        result["errors"] = [_issue("SCF_NOT_CONVERGED", "SCF/electronic-minimisation convergence failed.")]
    elif markers["fatal_error"]:
        result["classification"] = "fatal_error"
        result["errors"] = [_issue("FATAL_ERROR", "Fatal/error evidence prevents completion qualification.")]
    elif not completion_proven:
        result["classification"] = "output_truncated"
        result["errors"] = [_issue("COMPLETION_EVIDENCE_INCOMPLETE", "Finite final energy and total-time evidence are both required.")]
    elif expected_sha256 is None:
        result["classification"] = "output_unbound"
        result["errors"] = [_issue("EXTERNAL_OUTPUT_HASH_REQUIRED", "Completed classification requires a caller-supplied matching output SHA-256.")]
    else:
        result["status"] = "completed"
        result["classification"] = "completed"
        result["energy"] = {"unit": "eV", "value": energy}
    return result
