from __future__ import annotations

import json
from pathlib import Path
import unittest
from unittest.mock import patch

from materials_studio_mcp import castep_p4b_contract as p4b
from materials_studio_mcp.capability_registry import load_capability_registry
from materials_studio_mcp.public_registry import PUBLIC_TOOLS


P3C_PLAN = (
    Path(__file__).parents[1]
    / "docs"
    / "validation"
    / "receipts"
    / "p3c-corrected-real-castep-qualification-plan.json"
)


class CastepP4BContractTests(unittest.TestCase):
    def _manifest(self) -> Path:
        plan = json.loads(P3C_PLAN.read_text(encoding="utf-8"))
        return Path(plan["input"]["hashes"]["manifest_path"])

    def test_contract_is_deterministic_unregistered_and_nonexecuting(self) -> None:
        first = p4b.build_fixed_profile_public_api_contract()
        second = p4b.build_fixed_profile_public_api_contract()
        self.assertEqual(first, second)
        self.assertEqual(first["public_registration_state"], "not_registered")
        self.assertFalse(first["preflight"]["execution_allowed"])
        self.assertTrue(first["execution"]["blocked"])
        self.assertFalse(first["execution"]["implemented"])
        self.assertEqual(first["rollback"]["rollback_action"], "do_not_register_or_deploy_the_reserved_interface")
        self.assertTrue(first["rollback"]["retired_p3c_plan_must_not_be_reactivated"])

    def test_contract_rejects_arbitrary_parameters_and_requires_new_authorization(self) -> None:
        contract = p4b.build_fixed_profile_public_api_contract()
        request = contract["preflight"]["request_schema"]
        self.assertFalse(request["additionalProperties"])
        self.assertEqual(set(request["properties"]), {"input_manifest", "input_manifest_sha256"})
        execution = contract["execution"]
        self.assertIn("new_explicit_user_authorization", execution["requires"])
        self.assertIn("single_use_public_confirmation_token", execution["requires"])
        self.assertIn("automatic_retries", contract["non_goals"])

    def test_exact_p3c_manifest_preflight_passes_without_execution(self) -> None:
        manifest = self._manifest()
        result = p4b.inspect_fixed_profile_preflight_request(
            input_manifest=manifest,
            input_manifest_sha256=p4b._INPUT_HASHES["manifest_sha256"],
        )
        self.assertEqual(result["status"], "fixed_profile_preflight_pass")
        self.assertFalse(result["execution_allowed"])
        self.assertTrue(result["requires_new_execution_authorization"])
        self.assertEqual(result["public_registration_state"], "not_registered")
        self.assertEqual(result["input_hashes"], p4b._INPUT_HASHES)
        self.assertEqual(len(result["request_sha256"]), 64)

    def test_hash_or_manifest_contract_drift_fails_closed(self) -> None:
        manifest = self._manifest()
        with self.assertRaises(ValueError):
            p4b.inspect_fixed_profile_preflight_request(
                input_manifest=manifest, input_manifest_sha256="0" * 64
            )
        with patch.object(p4b, "_validate_input_contract", return_value=(
            "quartz_alpha_sp_4c", {**p4b._INPUT_HASHES, "param_sha256": "0" * 64}, []
        )):
            with self.assertRaises(ValueError):
                p4b.inspect_fixed_profile_preflight_request(
                    input_manifest=manifest,
                    input_manifest_sha256=p4b._INPUT_HASHES["manifest_sha256"],
                )

    def test_p4b_preserves_current_public_and_general_capability_boundary(self) -> None:
        self.assertEqual(len(PUBLIC_TOOLS), 50)
        self.assertIn(
            "ms_castep_fixed_profile_preflight",
            {item.name for item in PUBLIC_TOOLS},
        )
        capabilities = {item["id"]: item for item in load_capability_registry()["capabilities"]}
        self.assertFalse(capabilities["castep.calculation"]["verified"])
        self.assertFalse(capabilities["results.castep_parsing"]["verified"])


if __name__ == "__main__":
    unittest.main()
