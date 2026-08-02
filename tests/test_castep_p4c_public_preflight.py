from __future__ import annotations

import json
from pathlib import Path
import unittest

from materials_studio_mcp import server
from materials_studio_mcp.public_registry import PUBLIC_TOOLS


P3C_PLAN = (
    Path(__file__).parents[1]
    / "docs"
    / "validation"
    / "receipts"
    / "p3c-corrected-real-castep-qualification-plan.json"
)


class CastepP4CPublicPreflightTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.plan = json.loads(P3C_PLAN.read_text(encoding="utf-8"))
        cls.manifest = Path(cls.plan["input"]["hashes"]["manifest_path"])
        cls.manifest_sha256 = cls.plan["input"]["hashes"]["manifest_sha256"]

    def test_public_tool_is_r0_and_exact_profile_preflight_is_nonexecuting(self) -> None:
        registry = {item.name: item for item in PUBLIC_TOOLS}
        self.assertEqual(registry["ms_castep_fixed_profile_preflight"].risk, "R0")
        result = server.ms_castep_fixed_profile_preflight(
            str(self.manifest), self.manifest_sha256
        )
        self.assertTrue(result["ok"])
        data = result["data"]
        self.assertEqual(data["status"], "fixed_profile_preflight_pass")
        self.assertFalse(data["execution_allowed"])
        self.assertTrue(data["requires_new_execution_authorization"])
        self.assertEqual(data["public_registration_state"], "not_registered")
        self.assertEqual(len(data["request_sha256"]), 64)

    def test_hash_drift_is_a_public_input_error_without_runner_invocation(self) -> None:
        result = server.ms_castep_fixed_profile_preflight(
            str(self.manifest), "0" * 64
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "invalid_request")

    def test_preflight_does_not_accept_execution_controls(self) -> None:
        with self.assertRaises(TypeError):
            server.ms_castep_fixed_profile_preflight(
                str(self.manifest),
                self.manifest_sha256,
                cores=4,  # type: ignore[call-arg]
            )


if __name__ == "__main__":
    unittest.main()
