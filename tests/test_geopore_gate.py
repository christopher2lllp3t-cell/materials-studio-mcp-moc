from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from materials_studio_mcp.geopore_gate import assess_geopore_contract


INTAKE = Path(r"D:\分子动力学模拟\07_mcp_materials_studio\production_gates\G06_quartz_nanopore\intake_contract.json")


class GeoporeGateTests(unittest.TestCase):
    def test_current_intake_is_blocked_and_never_builds_defective_slab(self) -> None:
        result = assess_geopore_contract(str(INTAKE))
        self.assertEqual(result["status"], "blocked", result)
        self.assertFalse(result["construction_released"])
        self.assertEqual(result["errors"], [], result)
        self.assertEqual(result["semantic_decisions"]["verified_literature_sources"], 6)
        self.assertEqual(result["semantic_decisions"]["verified_visual_reviews"], 18)
        joined = "\n".join(result["blockers"])
        self.assertNotIn("Miller index", joined)
        self.assertIn("Hydroxylation", joined)
        self.assertNotIn("Positive geometric pore width", joined)
        self.assertIn("Pore-width reference_plane_definition", joined)
        self.assertIn("Electrostatics mode", joined)

    def test_literature_evidence_hash_mismatch_fails_closed(self) -> None:
        data = json.loads(INTAKE.read_text(encoding="utf-8"))
        data["literature_evidence"]["sha256"] = "0" * 64
        with tempfile.TemporaryDirectory(dir=r"E:\ms_mcp\ms_mcp_jobs") as temp:
            path = Path(temp) / "contract.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            result = assess_geopore_contract(str(path))
        self.assertEqual(result["status"], "fail")
        self.assertFalse(result["construction_released"])
        self.assertIn("Literature evidence SHA-256 mismatch", result["errors"])

    def test_contract_cannot_diverge_from_hashed_literature_decision(self) -> None:
        data = json.loads(INTAKE.read_text(encoding="utf-8"))
        data["cleavage"]["miller_index"] = [0, 0, 1]
        with tempfile.TemporaryDirectory(dir=r"E:\ms_mcp\ms_mcp_jobs") as temp:
            path = Path(temp) / "contract.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            result = assess_geopore_contract(str(path))
        self.assertEqual(result["status"], "fail")
        self.assertIn("Contract Miller index does not match hashed literature evidence", result["errors"])

    def test_unknown_fields_fail_closed(self) -> None:
        data = json.loads(INTAKE.read_text(encoding="utf-8"))
        data["build_anyway"] = True
        with tempfile.TemporaryDirectory(dir=r"E:\ms_mcp\ms_mcp_jobs") as temp:
            path = Path(temp) / "contract.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            result = assess_geopore_contract(str(path))
        self.assertEqual(result["status"], "fail")
        self.assertFalse(result["construction_released"])


if __name__ == "__main__":
    unittest.main()
