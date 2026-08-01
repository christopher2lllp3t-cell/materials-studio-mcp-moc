from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from materials_studio_mcp.science_contract import validate_science_contract
from materials_studio_mcp.project_manager import initialize_project, transition_project_status


HASH_A = "a" * 64
WORKSPACE_ROOT = Path(r"D:\分子动力学模拟")


def reviewed_manifest() -> dict:
    return {
        "project": {"status": "specified"},
        "geology_model": {"fidelity": "reviewed_scientific_model", "clay_is_surrogate": False},
        "science_contract": {
            "schema_version": 1,
            "units": {"profile": "ms_compass_to_lammps_real_v1", "lammps_style": "real",
                      "length": "angstrom", "time": "femtosecond", "energy": "kcal/mol",
                      "charge": "elementary_charge", "mass": "g/mol", "pressure": "atm"},
            "coordinates": {"representation": "fractional", "length_unit": "angstrom",
                            "cell_matrix_convention": "row_vectors_a_b_c"},
            "boundary": {"periodic_axes": ["x", "y", "z"], "lammps_boundary": ["p", "p", "p"]},
            "lammps": {"atom_style": "full", "triclinic_tilt_handling": "preserve"},
            "charge": {"formal_charge_semantics": "oxidation_or_connectivity_only",
                       "partial_charge_semantics": "forcefield_nonbonded_charge",
                       "partial_charge_source": "reviewed-profile", "expected_net_partial_charge_e": 0.0,
                       "audit_by_component": True},
            "forcefield": {"profile_id": "geo-reviewed-v1", "profile_version": "1.0.0",
                           "profile_sha256": HASH_A, "parameter_file_sha256": [HASH_A],
                           "compatibility_reviewed": True, "mixing_rule": "profile-defined",
                           "special_bonds": "profile-defined", "long_range_electrostatics": "pppm"},
            "seed_ledger": {"master_seed": 20260713,
                            "derivation": "sha256_run_uuid_stage_replica_to_int31_v1", "replica_index": 0,
                            "streams": {"packing": 101, "velocity": 102, "thermostat": 103, "sorption": 104}},
            "trajectory": {"wrapped_fields": "x_y_z", "unwrapped_fields": "xu_yu_zu",
                           "msd_uses_unwrapped": True, "density_uses_wrapped": True,
                           "timestep_unit": "femtosecond"},
        },
    }


class ScienceContractTests(unittest.TestCase):
    def test_complete_reviewed_contract_allows_production(self) -> None:
        result = validate_science_contract(reviewed_manifest())
        self.assertEqual(result["status"], "pass")
        self.assertTrue(result["production_allowed"])

    def test_reviewed_pcff_nonperiodic_contract_is_supported(self) -> None:
        manifest = reviewed_manifest()
        contract = manifest["science_contract"]
        contract["units"]["profile"] = "ms_pcff_to_lammps_real_v1"
        contract["coordinates"]["representation"] = "cartesian"
        contract["boundary"] = {"periodic_axes": [], "lammps_boundary": ["f", "f", "f"]}
        result = validate_science_contract(manifest)
        self.assertEqual(result["status"], "pass")
        self.assertTrue(result["production_allowed"])

    def test_units_pbc_atom_style_and_charge_semantics_fail_closed(self) -> None:
        manifest = reviewed_manifest()
        contract = manifest["science_contract"]
        contract["units"]["lammps_style"] = "metal"
        contract["boundary"] = {"periodic_axes": ["x", "y"], "lammps_boundary": ["p", "p", "p"]}
        contract["lammps"]["atom_style"] = "atomic"
        contract["charge"]["partial_charge_semantics"] = "formal_charge"
        result = validate_science_contract(manifest)
        joined = "\n".join(result["errors"])
        for expected in ("lammps_style", "periodic_axes", "atom_style", "partial charge semantics"):
            self.assertIn(expected, joined)
        self.assertFalse(result["production_allowed"])

    def test_forcefield_hashes_seed_and_unwrapped_semantics_are_mandatory(self) -> None:
        manifest = reviewed_manifest()
        contract = manifest["science_contract"]
        contract["forcefield"]["profile_sha256"] = "not-a-hash"
        contract["forcefield"]["compatibility_reviewed"] = False
        contract["seed_ledger"]["streams"].pop("sorption")
        contract["trajectory"]["unwrapped_fields"] = None
        result = validate_science_contract(manifest)
        joined = "\n".join(result["errors"])
        for expected in ("profile_sha256", "compatibility_reviewed", "streams", "unwrapped"):
            self.assertIn(expected, joined)

    def test_surrogate_clay_is_automatically_blocked_from_production(self) -> None:
        manifest = reviewed_manifest()
        manifest["project"]["status"] = "production"
        manifest["geology_model"] = {"fidelity": "surrogate_demo", "clay_is_surrogate": True,
                                     "source_model": "mica_2d_layer.xsd"}
        result = validate_science_contract(manifest)
        self.assertEqual(result["status"], "fail")
        self.assertTrue(result["surrogate_model"])
        self.assertFalse(result["production_allowed"])
        self.assertIn("SURROGATE_MODEL_BLOCKS_PRODUCTION", result["errors"])

    def test_surrogate_is_visible_even_before_production(self) -> None:
        manifest = reviewed_manifest()
        manifest["geology_model"] = {"fidelity": "surrogate_demo", "clay_is_surrogate": True}
        result = validate_science_contract(manifest)
        self.assertEqual(result["status"], "pass")
        self.assertFalse(result["production_allowed"])
        self.assertTrue(any("SURROGATE_MODEL" in item for item in result["warnings"]))

    def test_state_machine_cannot_transition_surrogate_to_production(self) -> None:
        with tempfile.TemporaryDirectory(prefix="surrogate_transition_", dir=WORKSPACE_ROOT / "tmp") as temporary:
            project = Path(initialize_project("surrogate", "Surrogate", projects_root=temporary)["project_directory"])
            manifest_path = project / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            source = reviewed_manifest()
            manifest["science_contract"] = source["science_contract"]
            manifest["geology_model"] = {"fidelity": "surrogate_demo", "clay_is_surrogate": True}
            manifest["project"]["status"] = "preflight_passed"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(PermissionError, "SURROGATE_MODEL"):
                transition_project_status(str(project), "production", reason="must not pass")
            unchanged = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(unchanged["project"]["status"], "preflight_passed")


if __name__ == "__main__":
    unittest.main()
