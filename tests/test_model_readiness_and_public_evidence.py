from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from materials_studio_mcp import model_readiness as readiness
from materials_studio_mcp import public_evidence as evidence
from materials_studio_mcp import server
from materials_studio_mcp.capability_registry import load_capability_registry
from materials_studio_mcp.public_registry import PUBLIC_TOOLS


def write_xsd(path: Path) -> None:
    path.write_text(
        "<XSD>\n"
        '<Atom3d ID="1" Name="Si1" Components="Si" XYZ="0.1,0.1,0.1" FormalCharge="0" />\n'
        '<SpaceGroup GroupName="P1" ITNumber="1" Operators="1,0,0,0,0,1,0,0,0,0,1,0" '
        'AVector="5,0,0" BVector="0,5,0" CVector="0,0,5" />\n'
        "</XSD>\n",
        encoding="utf-8",
    )


class ModelReadinessTests(unittest.TestCase):
    def _resolver(self, value: str) -> Path:
        return Path(value).resolve()

    def test_empty_spec_is_blocked_without_any_write_or_execution(self) -> None:
        result = readiness.assess_model_readiness({}, path_resolver=self._resolver, forcefield_root=Path("missing"))
        self.assertEqual(result["readiness"], "blocked")
        self.assertFalse(result["execution_allowed"])
        self.assertEqual(result["scientific_validity"], "not_determined")
        self.assertEqual({item["id"] for item in result["blockers"]}, {"model_class", "component_inventory", "target", "structure_source"})

    def test_complete_mechanical_forcite_intake_is_ready_but_not_scientifically_released(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            structure = root / "quartz.xsd"
            write_xsd(structure)
            spec = {
                "label": "quartz intake",
                "model_class": "crystal_bulk",
                "components": [{"name": "silica", "role": "host", "count": 1}],
                "structure": {"path": str(structure), "format": "xsd"},
                "target": {"engine": "forcite", "purpose": "energy"},
                "forcefield": {
                    "name": "compassiii", "charge_model": "forcefield_assigned",
                    "compatibility_review": "reviewed_literature",
                },
                "system_charge": {"net_charge": 0.0},
            }
            result = readiness.assess_model_readiness(spec, path_resolver=self._resolver, forcefield_root=root / "frc")
        self.assertEqual(result["readiness"], "ready")
        self.assertFalse(result["execution_allowed"])
        geometry = next(item for item in result["checks"] if item["id"] == "structure_geometry")
        self.assertEqual(geometry["details"]["periodic_dimension"], 3)

    def test_local_forcefield_is_hashed_but_not_promoted_to_scientific_approval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frc = root / "frc"
            frc.mkdir()
            selected = frc / "clayff.frc"
            selected.write_text("candidate force field\n", encoding="ascii")
            expected_sha256 = hashlib.sha256(selected.read_bytes()).hexdigest().upper()
            spec = {
                "model_class": "clay_mineral",
                "components": [{"name": "montmorillonite", "role": "host", "count": 1, "formal_charge": -1}],
                "target": {"engine": "lammps", "purpose": "conversion"},
                "forcefield": {"name": "ClayFF", "source_file": "clayff.frc", "charge_model": "provided"},
                "system_charge": {"net_charge": 0.0},
            }
            result = readiness.assess_model_readiness(spec, path_resolver=self._resolver, forcefield_root=frc)
        integrity = next(item for item in result["checks"] if item["id"] == "forcefield_integrity")
        self.assertEqual(integrity["status"], "pass")
        self.assertEqual(integrity["details"]["sha256"], expected_sha256)
        self.assertEqual(result["readiness"], "blocked")  # structure is deliberately still absent
        self.assertEqual(result["local_forcefield_candidates"][0]["scientific_status"], "candidate_only_requires_compatibility_review")

    def test_hash_drift_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.cif"
            source.write_text("data_test\n", encoding="ascii")
            spec = {
                "model_class": "crystal_bulk",
                "components": [{"name": "test", "count": 1}],
                "structure": {"path": str(source), "sha256": "0" * 64},
                "target": {"engine": "structure_only", "purpose": "build_only"},
            }
            result = readiness.assess_model_readiness(spec, path_resolver=self._resolver, forcefield_root=Path("missing"))
        self.assertEqual(result["readiness"], "blocked")
        self.assertIn("structure_integrity", {item["id"] for item in result["blockers"]})

    def test_database_record_creates_explicit_network_resolvable_gap(self) -> None:
        spec = {
            "model_class": "organic_condensed_phase",
            "components": [{"name": "benzene", "role": "solute", "count": 1}],
            "structure": {"source_kind": "database_record", "format": "sdf"},
            "target": {"engine": "structure_only", "purpose": "build_only"},
        }
        result = readiness.assess_model_readiness(spec, path_resolver=self._resolver, forcefield_root=Path("missing"))
        self.assertEqual(result["readiness"], "resolvable")
        gap = next(item for item in result["resolvable_gaps"] if item["id"] == "structure_source")
        self.assertEqual(gap["resolution"], "explicit_public_metadata_search")

    def test_general_castep_request_is_still_blocked(self) -> None:
        spec = {
            "model_class": "crystal_bulk",
            "components": [{"name": "alpha quartz", "count": 1}],
            "target": {"engine": "castep", "purpose": "dft_single_point"},
        }
        result = readiness.assess_model_readiness(spec, path_resolver=self._resolver, forcefield_root=Path("missing"))
        self.assertIn("castep_execution_boundary", {item["id"] for item in result["blockers"]})

    def test_search_is_bounded_to_requested_local_roots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "one.cif").write_text("data\n", encoding="ascii")
            (root / "skip.txt").write_text("not a structure", encoding="ascii")
            result = readiness.discover_local_structure_sources([str(root)], path_resolver=self._resolver)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["relative_path"], "one.cif")
        self.assertTrue(result[0]["selection_required"])

    def test_gap_plan_never_claims_automatic_parameterization(self) -> None:
        spec = {
            "model_class": "organic_condensed_phase",
            "components": [{"name": "benzene", "count": 1}],
            "target": {"engine": "forcite", "purpose": "dynamics"},
        }
        plan = readiness.build_model_gap_resolution_plan(spec, path_resolver=self._resolver, forcefield_root=Path("missing"))
        self.assertFalse(plan["execution_allowed"])
        self.assertIn("md_search_public_model_evidence", {item["public_tool"] for item in plan["actions"]})
        self.assertIn("partial charges", " ".join(plan["automatic_resolution_boundary"]).lower())

    def test_unknown_spec_field_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unknown model_spec fields"):
            readiness.assess_model_readiness({"invent_parameter": True}, path_resolver=self._resolver)


class PublicEvidenceTests(unittest.TestCase):
    def test_request_plan_uses_only_fixed_https_provider_hosts(self) -> None:
        pubchem = evidence.build_public_evidence_request("benzene", "pubchem", 1)
        crossref = evidence.build_public_evidence_request("ClayFF force field", "crossref", 2)
        self.assertEqual(pubchem["expected_host"], "pubchem.ncbi.nlm.nih.gov")
        self.assertTrue(pubchem["source_url"].startswith("https://pubchem.ncbi.nlm.nih.gov/"))
        self.assertEqual(crossref["expected_host"], "api.crossref.org")
        self.assertTrue(crossref["request_constraints"]["redirects_allowed"] is False)

    def test_pubchem_response_is_normalized_without_download(self) -> None:
        payload = {"PropertyTable": {"Properties": [{
            "CID": 241, "MolecularFormula": "C6H6", "MolecularWeight": 78.11,
            "CanonicalSMILES": "C1=CC=CC=C1", "InChIKey": "UHOVQNZJYSORNB-UHFFFAOYSA-N",
        }]}}
        with patch.object(evidence, "_open_provider_json", return_value=payload) as open_provider:
            result = evidence.search_public_model_evidence("benzene", "pubchem", 1)
        open_provider.assert_called_once()
        self.assertEqual(result["record_count"], 1)
        self.assertEqual(result["records"][0]["cid"], 241)
        self.assertIn("record/SDF", result["records"][0]["candidate_sdf_url"])
        self.assertEqual(result["network_receipt"]["download_or_execution"], "not_performed")

    def test_crossref_response_is_normalized_without_raw_provider_body(self) -> None:
        payload = {"message": {"items": [{
            "DOI": "10.1000/example", "title": ["Example method"], "container-title": ["Journal"],
            "published": {"date-parts": [[2024, 1, 1]]}, "author": [{"given": "Ada", "family": "Lovelace"}],
            "URL": "https://doi.org/10.1000/example", "type": "journal-article", "score": 12.5,
        }]}}
        with patch.object(evidence, "_open_provider_json", return_value=payload):
            result = evidence.search_public_model_evidence("Example method", "crossref", 1)
        self.assertEqual(result["records"][0]["doi"], "10.1000/example")
        self.assertEqual(result["records"][0]["authors"], ["Ada Lovelace"])
        self.assertNotIn("message", repr(result))

    def test_invalid_provider_and_oversized_result_limit_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            evidence.build_public_evidence_request("benzene", "arbitrary_url", 1)
        with self.assertRaises(ValueError):
            evidence.build_public_evidence_request("benzene", "pubchem", 6)

    def test_server_dry_run_never_calls_network_search(self) -> None:
        with patch.object(server, "search_public_model_evidence", side_effect=AssertionError("network must not be called")):
            result = server.md_search_public_model_evidence("benzene", "pubchem")
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["network_access"], "not_requested")
        self.assertTrue(result["data"]["dry_run"])

    def test_server_live_lookup_requires_opt_in_and_exact_confirmation(self) -> None:
        denied = server.md_search_public_model_evidence("benzene", "pubchem", dry_run=False)
        self.assertFalse(denied["ok"])
        self.assertEqual(denied["error"]["code"], "permission_denied")
        parameters = {"query": "benzene", "provider": "pubchem", "max_results": 1, "allow_network": True}
        issued = server.md_prepare_production_confirmation("md_search_public_model_evidence", parameters, 60)
        with patch.object(server, "search_public_model_evidence", return_value={"record_count": 0, "records": []}) as search:
            accepted = server.md_search_public_model_evidence(
                "benzene", "pubchem", max_results=1, allow_network=True, dry_run=False,
                confirmation_token=issued["confirmation_token"],
            )
        self.assertTrue(accepted["ok"])
        search.assert_called_once_with("benzene", "pubchem", 1)

    def test_public_registry_and_catalog_expose_the_three_reviewed_tools(self) -> None:
        names = {item.name for item in PUBLIC_TOOLS}
        self.assertTrue({
            "md_model_readiness_assess", "md_model_gap_resolution_plan", "md_search_public_model_evidence",
        }.issubset(names))
        catalog = {entry["tool"] for entry in server.ms_task_catalog()["workflows"]}
        self.assertTrue({
            "md_model_readiness_assess", "md_model_gap_resolution_plan", "md_search_public_model_evidence",
        }.issubset(catalog))

    def test_capability_registry_records_readonly_intake_boundary(self) -> None:
        capability = {
            item["id"]: item for item in load_capability_registry()["capabilities"]
        }["core.model_intake_readiness"]
        self.assertTrue(capability["verified"])
        self.assertEqual(capability["exposure"], "public")
        self.assertEqual(capability["api_symbols"], [])
        self.assertIn("does not generate", capability["notes"])


if __name__ == "__main__":
    unittest.main()
