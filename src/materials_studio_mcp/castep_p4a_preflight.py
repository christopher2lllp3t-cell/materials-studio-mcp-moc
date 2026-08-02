from __future__ import annotations

"""P4-A locale-safe environment and fixed-profile publication preflight.

This module does not launch CASTEP and is not MCP-visible. It isolates the
Windows Materials Studio Perl locale from inherited C.UTF-8 values that the
bundled Perl does not support.
"""

import hashlib
import os
from pathlib import Path
import subprocess
from typing import Any, Mapping

from .geology_modeling import sha256_file


POLICY_REVISION = "ms-mcp.p4a-castep-locale-and-publication-preflight.1.3.0-r1"
_MATERIALS_STUDIO_PERL = Path(
    r"D:\Program Files (x86)\BIOVIA\Materials Studio 23.1\bin\perl.exe"
)
_MATERIALS_STUDIO_PERL_SHA256 = (
    "31F0629C1A9A6489505376AA76AE5A742667615487F35338E8306DE1317EF95F"
)
_LOCALE_KEYS = ("LC_ALL", "LC_CTYPE", "LANG")
_SAFE_LOCALE = "C"
_P3C_PLAN_SHA256 = "10F3C622A161EAB3F25B0A9E19031AA9C485C7946E758CFDE5C1CD625B5F726B"
_P3C_RECEIPT_SHA256 = "12FB79B370A783618C5F0580192D2B40E459A4E6DD4D9875210CED05415EB872"
_P3C_OUTPUT_SHA256 = "EE91F3319375DEFD581644840F64718C066291027D2E837ACD7B6DCEB468E851"


def _selected_locale(environment: Mapping[str, str]) -> dict[str, str | None]:
    return {key: environment.get(key) for key in _LOCALE_KEYS}


def build_materials_studio_perl_environment(
    parent: Mapping[str, str] | None = None,
) -> tuple[dict[str, str], dict[str, Any]]:
    """Return a child-only environment without mutating the parent mapping."""

    source = os.environ if parent is None else parent
    if not isinstance(source, Mapping):
        raise TypeError("parent environment must be a string mapping")
    environment: dict[str, str] = {}
    for key, value in source.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise TypeError("parent environment keys and values must be strings")
        environment[key] = value
    before = _selected_locale(environment)
    for key in _LOCALE_KEYS:
        environment[key] = _SAFE_LOCALE
    receipt = {
        "schema_version": 1,
        "policy_revision": POLICY_REVISION,
        "scope": "child_process_only",
        "parent_environment_mutated": False,
        "locale_before": before,
        "locale_after": _selected_locale(environment),
        "windows_system_or_user_environment_mutated": False,
        "reason": "MS_2023_bundled_Perl_rejects_inherited_C_UTF_8_on_Windows_code_page_936",
    }
    return environment, receipt


def audit_materials_studio_perl_locale() -> dict[str, Any]:
    """Run a harmless fixed Perl print command; never starts CASTEP or a license."""

    perl = _MATERIALS_STUDIO_PERL.resolve(strict=True)
    observed = sha256_file(perl)
    if observed != _MATERIALS_STUDIO_PERL_SHA256:
        raise PermissionError("Materials Studio Perl SHA-256 differs from the reviewed P4-A value")
    environment, policy = build_materials_studio_perl_environment()
    completed = subprocess.run(
        [str(perl), "-e", "print qq(MS_PERL_LOCALE_OK\\n)"],
        cwd=perl.parent,
        env=environment,
        shell=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=10,
        check=False,
    )
    stdout = completed.stdout.decode("ascii", errors="replace").replace("\r\n", "\n")
    stderr = completed.stderr.decode("utf-8", errors="replace")
    warnings = [
        marker for marker in ("locale", "falling back", "setting locale failed")
        if marker in stderr.lower()
    ]
    return {
        "schema_version": 1,
        "policy_revision": POLICY_REVISION,
        "status": "pass" if completed.returncode == 0 and stdout == "MS_PERL_LOCALE_OK\n" and not warnings else "fail",
        "castep_or_license_started": False,
        "perl": {"path": str(perl), "sha256": observed, "bytes": perl.stat().st_size},
        "process": {"exit_code": completed.returncode, "shell": False, "timeout_seconds": 10},
        "environment_policy": policy,
        "stdout_sha256": hashlib.sha256(completed.stdout).hexdigest().upper(),
        "stderr_sha256": hashlib.sha256(completed.stderr).hexdigest().upper(),
        "stderr_bytes": len(completed.stderr),
        "locale_warning_markers": warnings,
    }


def build_fixed_profile_publication_preflight() -> dict[str, Any]:
    """Describe the only profile eligible for later P4-B review; no execution."""

    return {
        "schema_version": 1,
        "policy_revision": POLICY_REVISION,
        "status": "blocked_pending_p4b_public_api_review",
        "execution_allowed": False,
        "public_tool_added": False,
        "profile": {
            "material": "alpha_quartz_Si3O6_9_atoms",
            "task": "SinglePoint",
            "xc_functional": "PBE",
            "energy_cutoff_eV": 600.0,
            "kpoint_grid": [3, 3, 3],
            "pseudopotentials": "default_OTFG",
            "cores": 4,
            "timeout_seconds": 600,
            "scientific_scope": "platform_qualification_only_not_convergence_evidence",
        },
        "qualified_evidence": {
            "p3c_plan_sha256": _P3C_PLAN_SHA256,
            "p3c_runner_receipt_sha256": _P3C_RECEIPT_SHA256,
            "p3c_output_sha256": _P3C_OUTPUT_SHA256,
        },
        "locale_policy": {
            "child_only": True,
            "values": {key: _SAFE_LOCALE for key in _LOCALE_KEYS},
            "machine_or_user_environment_change": False,
        },
        "blockers": [
            "public API request and response schema not yet frozen",
            "confirmation and authorization lifecycle not yet reviewed",
            "deployment and rollback plan not yet reviewed",
            "general materials and caller-selected CASTEP parameters remain unsupported",
        ],
    }
