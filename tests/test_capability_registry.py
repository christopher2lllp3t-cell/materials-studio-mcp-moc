from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from materials_studio_mcp.capability_registry import audit_capability_registry, load_capability_registry


class CapabilityRegistryTests(unittest.TestCase):
    def test_registry_is_closed_and_hash_verified_on_this_ms_23_1_machine(self) -> None:
        result = audit_capability_registry()
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["summary"]["declared_verified"], result["summary"]["effective_verified"])
        self.assertFalse(result["policy"]["unregistered_is_verified"])
        self.assertFalse(result["policy"]["natural_language_to_perl"])
        self.assertEqual(result["policy"]["parameter_policy"], "closed_allowlist")

    def test_unimplemented_layers_are_explicitly_unverified_and_hidden(self) -> None:
        data = load_capability_registry()
        capabilities = {item["id"]: item for item in data["capabilities"]}
        for capability_id in (
            "castep.calculation",
            "results.castep_parsing",
            "reports.materialsscript_report_generation",
        ):
            item = capabilities[capability_id]
            self.assertFalse(item["verified"])
            self.assertEqual(item["exposure"], "not_implemented")

    def test_standalone_input_generation_is_registered_without_claiming_calculation(self) -> None:
        data = load_capability_registry()
        capabilities = {item["id"]: item for item in data["capabilities"]}
        candidate = capabilities["castep.standalone_input_generation"]
        self.assertTrue(candidate["verified"])
        self.assertEqual(candidate["exposure"], "public")
        self.assertEqual(candidate["api_symbols"], [])
        self.assertIn("never starts RunCASTEP", candidate["notes"])
        self.assertFalse(capabilities["castep.calculation"]["verified"])

    def test_npt_profile_is_registered_with_fixed_pressure_evidence(self) -> None:
        data = load_capability_registry()
        capabilities = {item["id"]: item for item in data["capabilities"]}
        candidate = capabilities["forcite.dynamics_npt"]
        self.assertTrue(candidate["verified"])
        self.assertEqual(candidate["exposure"], "public")
        parameter_names = {item["name"] for item in candidate["parameters"]}
        self.assertIn("Pressure", parameter_names)
        self.assertIn("Barostat", parameter_names)
        self.assertIn("1.01325e-4 GPa", candidate["notes"])

    def test_unknown_registry_fields_fail_closed(self) -> None:
        data = load_capability_registry()
        data["automatic_parameter_completion"] = True
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capabilities.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Unknown capability registry keys"):
                load_capability_registry(path)


if __name__ == "__main__":
    unittest.main()
