from __future__ import annotations

"""P4-B contracts for a future restricted public CASTEP fixed-profile API.

The contract is internal and nonexecuting.  No MCP tool is registered here.
It reserves an exact request/response shape for later P4-C review while keeping
the P3-C profile, user authorization, confirmation, and rollback boundaries
explicit.
"""

import hashlib
import json
from pathlib import Path
import re
from typing import Any

from .castep_p4a_preflight import (
    POLICY_REVISION as P4A_POLICY_REVISION,
    build_fixed_profile_publication_preflight,
)
from .castep_standalone_runner import _validate_input_contract
from .geology_modeling import sha256_file


CONTRACT_REVISION = "ms-mcp.p4b-fixed-castep-public-contract.1.3.0-r1"
PREFLIGHT_TOOL_NAME = "ms_castep_fixed_profile_preflight"
EXECUTION_TOOL_NAME = "ms_castep_fixed_profile_execute"
P3C_PLAN_SHA256 = "10F3C622A161EAB3F25B0A9E19031AA9C485C7946E758CFDE5C1CD625B5F726B"
P3C_RUNNER_RECEIPT_SHA256 = "12FB79B370A783618C5F0580192D2B40E459A4E6DD4D9875210CED05415EB872"
P3C_OUTPUT_SHA256 = "EE91F3319375DEFD581644840F64718C066291027D2E837ACD7B6DCEB468E851"
_INPUT_HASHES = {
    "manifest_sha256": "8CAF21ABEB448A6D2669AA10684362652B2E97A1677D8C1AC1682F11CECA1C79",
    "source_sha256": "12B9147B763EBD2BAB08F04F2D304E51DB422854B47F93890B52BFB2A1AEF8EE",
    "input_source_copy_sha256": "12B9147B763EBD2BAB08F04F2D304E51DB422854B47F93890B52BFB2A1AEF8EE",
    "cell_sha256": "010F86189B61DCCB7D1557BB1E9ECD3353C93A9575AC15AEEB473B49FF61C34E",
    "param_sha256": "31E5F02C9204429786B7C31A07A45EE1409697235D57CCE689D2AD2D87F24576",
    "contract_file_sha256": "9C461EB21B8E7D90040A9DCE13179D4F974E9B8F2B7F70E129D4F83B4AE4A03D",
    "contract_canonical_sha256": "ABB51030B44A697D78ADCE91595AC51A445D97F4B0E885E4CA9DCD73E3805A42",
}
_SHA256 = re.compile(r"^[0-9A-Fa-f]{64}$")


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("ascii")).hexdigest().upper()


def _require_sha256(value: str, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must contain exactly 64 hexadecimal characters")
    return value.upper()


def build_fixed_profile_public_api_contract() -> dict[str, Any]:
    """Return a deterministic, unregistered P4-B API/confirmation/rollback contract."""

    preflight_request = {
        "type": "object",
        "additionalProperties": False,
        "required": ["input_manifest", "input_manifest_sha256"],
        "properties": {
            "input_manifest": {"type": "string", "description": "Exact prepared P3-C standalone manifest path."},
            "input_manifest_sha256": {"type": "string", "pattern": "^[0-9A-Fa-f]{64}$"},
        },
    }
    preflight_response = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "status", "execution_allowed", "profile_id", "request_sha256",
            "requires_new_execution_authorization", "public_registration_state",
        ],
        "properties": {
            "status": {"const": "fixed_profile_preflight_pass"},
            "execution_allowed": {"const": False},
            "profile_id": {"const": "alpha_quartz_p3c_fixed_profile"},
            "request_sha256": {"type": "string", "pattern": "^[0-9A-F]{64}$"},
            "requires_new_execution_authorization": {"const": True},
            "public_registration_state": {"const": "not_registered"},
        },
    }
    execution_request = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "preflight_request_sha256", "confirmation_token",
            "external_single_use_authorization",
        ],
        "properties": {
            "preflight_request_sha256": {"type": "string", "pattern": "^[0-9A-F]{64}$"},
            "confirmation_token": {"type": "string", "minLength": 1},
            "external_single_use_authorization": {
                "type": "object",
                "description": "Future P4-C field; must bind a new plan, exact request, nonce, resources, and command serialization.",
            },
        },
    }
    payload = {
        "schema_version": 1,
        "contract_revision": CONTRACT_REVISION,
        "public_registration_state": "not_registered",
        "preflight": {
            "tool_name": PREFLIGHT_TOOL_NAME,
            "risk": "R0",
            "request_schema": preflight_request,
            "response_schema": preflight_response,
            "execution_allowed": False,
        },
        "execution": {
            "tool_name": EXECUTION_TOOL_NAME,
            "risk": "R3",
            "request_schema": execution_request,
            "implemented": False,
            "blocked": True,
            "requires": [
                "new_frozen_execution_plan",
                "new_explicit_user_authorization",
                "single_use_public_confirmation_token",
                "P4C_independent_review",
                "candidate_deployment_and_rollback_review",
            ],
        },
        "fixed_profile_evidence": {
            "p3c_plan_sha256": P3C_PLAN_SHA256,
            "p3c_runner_receipt_sha256": P3C_RUNNER_RECEIPT_SHA256,
            "p3c_output_sha256": P3C_OUTPUT_SHA256,
            "locale_policy_revision": P4A_POLICY_REVISION,
        },
        "rollback": {
            "candidate_only": True,
            "immutable_deployment_modified": False,
            "current_pointer_modified": False,
            "rollback_action": "do_not_register_or_deploy_the_reserved_interface",
            "retired_p3c_plan_must_not_be_reactivated": True,
        },
        "non_goals": [
            "arbitrary_materials",
            "caller_selected_Castep_parameters",
            "automatic_retries",
            "general_public_castep_calculation",
            "general_public_results_parsing",
        ],
    }
    return {**payload, "contract_sha256": _canonical_sha256(payload)}


def inspect_fixed_profile_preflight_request(
    *, input_manifest: Path, input_manifest_sha256: str
) -> dict[str, Any]:
    """Validate one exact P3-C input package for a future public preflight.

    This is filesystem-read-only and never invokes Perl, RunCASTEP, CASTEP,
    Materials Studio, Gateway, MPI, or a license.
    """

    manifest = Path(input_manifest).resolve(strict=True)
    supplied_hash = _require_sha256(input_manifest_sha256, "input_manifest_sha256")
    observed_hash = sha256_file(manifest)
    if supplied_hash != observed_hash or observed_hash != _INPUT_HASHES["manifest_sha256"]:
        raise ValueError("Only the exact P3-C standalone manifest SHA-256 is eligible")
    seedname, hashes, errors = _validate_input_contract(manifest)
    if errors or seedname != "quartz_alpha_sp_4c":
        raise ValueError("Standalone input contract is not the exact P3-C fixed profile")
    if hashes != _INPUT_HASHES:
        raise ValueError("Standalone input hashes differ from the exact P3-C fixed profile")
    p4a = build_fixed_profile_publication_preflight()
    if p4a["execution_allowed"] or p4a["public_tool_added"]:
        raise RuntimeError("P4-A boundary unexpectedly permits execution or a public tool")
    request_payload = {
        "contract_revision": CONTRACT_REVISION,
        "profile_id": "alpha_quartz_p3c_fixed_profile",
        "input_manifest_sha256": observed_hash,
        "input_hashes": hashes,
        "p3c_plan_sha256": P3C_PLAN_SHA256,
        "p3c_runner_receipt_sha256": P3C_RUNNER_RECEIPT_SHA256,
        "p3c_output_sha256": P3C_OUTPUT_SHA256,
    }
    return {
        "schema_version": 1,
        "contract_revision": CONTRACT_REVISION,
        "status": "fixed_profile_preflight_pass",
        "execution_allowed": False,
        "profile_id": "alpha_quartz_p3c_fixed_profile",
        "request_sha256": _canonical_sha256(request_payload),
        "input_manifest_sha256": observed_hash,
        "input_hashes": hashes,
        "requires_new_execution_authorization": True,
        "requires_public_confirmation_token_if_execution_is_implemented": True,
        "public_registration_state": "not_registered",
        "rollback_state": "candidate_only_no_deployment_or_current_pointer_change",
        "blockers": build_fixed_profile_public_api_contract()["execution"]["requires"],
    }
