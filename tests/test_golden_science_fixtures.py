from __future__ import annotations

import json
import math
import unittest
from pathlib import Path


GOLDEN_ROOT = Path(r"D:\分子动力学模拟\07_mcp_materials_studio\golden_science")


class GoldenScienceFixtureTests(unittest.TestCase):
    def test_g01_through_g06_have_versioned_nonproduction_manifests(self) -> None:
        manifests = sorted(GOLDEN_ROOT.glob("G*/manifest.json"))
        self.assertEqual(len(manifests), 6)
        for index, path in enumerate(manifests, 1):
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["golden_id"], f"G{index:02d}")
            self.assertGreaterEqual(data["fixture_version"], 1)
            self.assertEqual(data["definition_status"], "complete")
            # A guarded short chain may pass while production remains closed.
            # Only G01 may claim a completed runtime calibration, and that claim
            # must be explicitly scoped and backed by an existing evidence file.
            allowed = {"pending", "short_chain_pass"}
            if index == 1:
                allowed.add("pass")
            self.assertIn(data["runtime_status"], allowed)
            if data["runtime_status"] == "pass":
                self.assertEqual(index, 1)
                self.assertIn("calibration fixture only", data["scope"])
                self.assertTrue(Path(data["evidence"]).is_file())
                self.assertIn("claim global PCFF 3.1/4.0 equivalence", data["prohibited"])
            self.assertTrue(data["required_gates"])
            self.assertTrue(data["prohibited"])

    def test_g04_explicitly_prohibits_surrogate_production(self) -> None:
        data = json.loads((GOLDEN_ROOT / "G04_charged_clay" / "manifest.json").read_text(encoding="utf-8"))
        self.assertFalse(data["contract"]["surrogate_allowed_in_production"])
        self.assertIn("mica surrogate in production", data["prohibited"])

    def test_g05_reference_distance_is_independently_recomputable(self) -> None:
        data = json.loads((GOLDEN_ROOT / "G05_triclinic_cell" / "manifest.json").read_text(encoding="utf-8"))
        cell = data["reference"]["cell_rows_angstrom"]
        delta = data["expected"]["minimum_image_fractional_delta"]
        cart = [sum(delta[row] * cell[row][column] for row in range(3)) for column in range(3)]
        distance = math.sqrt(sum(value * value for value in cart))
        self.assertAlmostEqual(distance, data["expected"]["minimum_image_distance_angstrom"], places=12)


if __name__ == "__main__":
    unittest.main()
