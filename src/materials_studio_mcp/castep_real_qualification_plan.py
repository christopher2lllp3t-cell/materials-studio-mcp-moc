from __future__ import annotations

"""Private P3-A plan for one bounded real CASTEP qualification attempt.

This module is deliberately non-executing.  It validates exact local launcher
and input evidence, renders a deterministic command preview as data, and keeps
the real execution entry permanently blocked pending a separate user approval
and a reviewed P3-B implementation.
"""

import hashlib
import json
from pathlib import Path
import re
from typing import Any

from .castep_result_parser import _empty_result, _input_contract
from .geology_modeling import sha256_file


PLAN_SCHEMA_VERSION = 1
PLAN_REVISION = "ms-mcp.private-real-castep-qualification-plan.1.3.0-p3a-r1"

_RUNCASTEP = Path(r"D:\Program Files (x86)\BIOVIA\Materials Studio 23.1\etc\CASTEP\bin\RunCASTEP.bat")
_RUNCASTEP_SHA256 = "FE09BD22E729E03D1B75027CAC9ECF2A0CC250A170FE5EE309CC33CCD070C027"
_RUNCASTEP_README = _RUNCASTEP.with_name("RunCASTEP.Readme")
_RUNCASTEP_README_SHA256 = "08F9E048CFC9D9C98054D77EEB7F0AF3A2AD61F79182B2E9D81CC75A57D1712B"
_CMD = Path(r"C:\Windows\System32\cmd.exe")
_CMD_SHA256 = "65EC268ADD3973B6DCA64222985DA47CAEAEE44A340B0EC1466782914FD743D9"
_QUALIFICATION_ROOT = Path(r"E:\ms_mcp\ms_mcp_jobs\castep_real_qualification")

_SAFE_SEED = re.compile(r"^[A-Za-z0-9_]{1,48}$")
_EXPECTED_SETTINGS = {
    "task": "SinglePoint",
    "xc_functional": "PBE",
    "energy_cutoff_ev": 600.0,
    "kpoint_mp_grid": [3, 3, 3],
    "spin_polarized": False,
    "fix_occupancy": True,
    "species_pot": "omitted_standard_elements_use_default_otfg",
    "cores": 4,
}


def _issue(code: str, detail: str) -> dict[str, str]:
    return {"code": code, "detail": detail}


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest().upper()


def _fixed_file(path: Path, expected_sha256: str, label: str) -> dict[str, Any]:
    resolved = path.resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"Fixed {label} is missing: {resolved}")
    observed = sha256_file(resolved)
    if observed != expected_sha256:
        raise PermissionError(f"Fixed {label} SHA-256 does not match the reviewed P3-A value")
    return {"path": str(resolved), "sha256": observed, "bytes": resolved.stat().st_size}


def _manifest_data(input_manifest: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest_path = input_manifest.resolve()
    parser_envelope = _empty_result(None, None, None)
    seedname, errors = _input_contract(manifest_path, parser_envelope)
    if errors or seedname is None:
        codes = ", ".join(item["code"] for item in errors) or "INPUT_CONTRACT_INVALID"
        raise ValueError(f"Standalone input contract is not valid for P3-A: {codes}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if _SAFE_SEED.fullmatch(seedname) is None:
        raise ValueError("P3-A requires an ASCII-safe standalone seedname")
    source = manifest.get("source")
    settings = manifest.get("settings")
    if not isinstance(source, dict) or not isinstance(settings, dict):
        raise ValueError("P3-A input manifest is missing source or settings evidence")
    if source.get("runtime_atom_count") != 9 or source.get("elements") != {"O": 6, "Si": 3}:
        raise ValueError("P3-A is frozen to the reviewed 9-atom alpha-quartz Si3O6 candidate")
    if settings != _EXPECTED_SETTINGS:
        raise ValueError("P3-A settings differ from the reviewed qualification-only candidate")
    return manifest, {
        "manifest_path": str(manifest_path),
        **parser_envelope["input_hashes"],
    }


def build_real_castep_qualification_plan(*, input_manifest: Path) -> dict[str, Any]:
    """Build a deterministic, execution-blocked plan for later user review."""

    manifest, input_hashes = _manifest_data(Path(input_manifest))
    root = _QUALIFICATION_ROOT.resolve()
    if not str(root).isascii():
        raise ValueError("P3-A qualification root must be a complete ASCII path")

    launcher = _fixed_file(_RUNCASTEP, _RUNCASTEP_SHA256, "RunCASTEP launcher")
    readme = _fixed_file(_RUNCASTEP_README, _RUNCASTEP_README_SHA256, "RunCASTEP documentation")
    command_interpreter = _fixed_file(_CMD, _CMD_SHA256, "Windows command interpreter")
    seedname = manifest["seedname"]
    reviewed_command = f'call "{launcher["path"]}" -np 4 {seedname}'

    plan: dict[str, Any] = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "plan_revision": PLAN_REVISION,
        "plan_type": "single_local_castep_platform_qualification",
        "status": "blocked_pending_separate_user_authorization",
        "execution_allowed": False,
        "qualification_only": True,
        "production_science_released": False,
        "input": {
            "seedname": seedname,
            "hashes": input_hashes,
            "runtime_atom_count": 9,
            "elements": {"O": 6, "Si": 3},
            "settings": _EXPECTED_SETTINGS,
            "scientific_interpretation": "syntax_and_platform_qualification_only_not_convergence_evidence",
        },
        "runtime": {
            "mode": "local",
            "queue": None,
            "cores": 4,
            "max_parallel_jobs": 1,
            "hard_timeout_seconds": 600,
            "qualification_root": str(root),
            "launcher": launcher,
            "launcher_documentation": readme,
            "command_interpreter": command_interpreter,
            "command_preview": [
                command_interpreter["path"], "/d", "/s", "/c", reviewed_command
            ],
            "command_preview_is_data_only": True,
        },
        "control_policy": {
            "new_non_overwriting_ascii_job_directory": True,
            "single_cross_process_execution_slot": True,
            "stdin": "closed",
            "stdout_stderr_hash_receipts": True,
            "staged_input_hash_check": ["before_launch", "after_exit"],
            "timeout_or_cancel": "terminate_only_owned_root_process_tree",
            "success_requires": [
                "launcher_exit_observed",
                "input_hashes_unchanged",
                "output_sha256_bound",
                "finite_final_energy",
                "finite_total_time",
                "no_failure_markers",
                "no_owned_processes_remaining",
            ],
        },
        "blockers": [
            _issue("SEPARATE_USER_EXECUTION_AUTHORIZATION_REQUIRED", "File-change approval does not authorize a licensed CASTEP calculation."),
            _issue("REAL_CASTEP_RUNNER_NOT_RELEASED", "The current real runner entry remains unconditionally blocked."),
            _issue("CASTEP_LICENSE_SEAT_UNVERIFIED", "A running license service does not prove that a CASTEP seat is available."),
            _issue("SCIENTIFIC_CONVERGENCE_NOT_ESTABLISHED", "600 eV and 3x3x3 are platform-qualification candidates, not research convergence evidence."),
        ],
        "future_single_use_authorization_contract": {
            "must_bind": [
                "plan_sha256",
                "input_manifest_sha256",
                "launcher_sha256",
                "command_interpreter_sha256",
                "cores",
                "timeout_seconds",
                "one_new_job_directory",
            ],
            "single_use": True,
            "not_issued_by_p3a": True,
        },
    }
    return {**plan, "plan_sha256": _canonical_sha256(plan)}


def validate_real_castep_qualification_plan(plan: dict[str, Any]) -> None:
    if not isinstance(plan, dict) or not isinstance(plan.get("plan_sha256"), str):
        raise ValueError("P3-A plan must be an object with plan_sha256")
    payload = {key: value for key, value in plan.items() if key != "plan_sha256"}
    if _canonical_sha256(payload) != plan["plan_sha256"]:
        raise ValueError("P3-A plan SHA-256 does not match its canonical payload")
    if plan.get("execution_allowed") is not False or plan.get("status") != "blocked_pending_separate_user_authorization":
        raise ValueError("P3-A plan must remain execution-blocked")


def execute_real_castep_qualification(*, plan: dict[str, Any]) -> dict[str, Any]:
    """Remain permanently blocked in P3-A; no subprocess path exists here."""

    validate_real_castep_qualification_plan(plan)
    return {
        "schema_version": PLAN_SCHEMA_VERSION,
        "plan_revision": PLAN_REVISION,
        "status": "blocked",
        "executed": False,
        "plan_sha256": plan["plan_sha256"],
        "blockers": plan["blockers"],
    }
