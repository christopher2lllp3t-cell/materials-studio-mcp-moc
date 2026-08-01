from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import json
import unittest

from materials_studio_mcp.project_manifest_v2 import (
    new_manifest_v2,
    register_artifact_v2,
    set_target_artifact_v2,
    transition_project_state_v2,
    validate_manifest_v2,
)


HASH = "a" * 64
HASH_B = "b" * 64


def artifact(artifact_id: str, artifact_type: str, parents: list[str], *, status: str = "VERIFIED", metadata: dict | None = None) -> dict:
    return {
        "artifact_id": artifact_id,
        "artifact_type": artifact_type,
        "path": f"artifacts/{artifact_id}.json",
        "sha256": HASH,
        "parent_artifact_ids": parents,
        "created_by": "unit-test",
        "tool_version": "test-1",
        "created_at": "2026-07-20T00:00:00+00:00",
        "status": status,
        **({"metadata": metadata} if metadata is not None else {}),
    }


def evidence(evidence_id: str, parent: str, gate: str, *, kind: str = "SOFTWARE_FUNCTION", role: str = "target", result: str = "PASS", status: str = "VERIFIED") -> dict:
    return artifact(evidence_id, "evidence_receipt", [parent], status=status, metadata={
        "evidence_kind": kind,
        "evidence_scope": "target_model" if role == "target" else "calibration_model",
        "model_role": role,
        "gate": gate,
        "result": result,
        "subject_artifact_ids": [parent],
    })


def forcefield(parent: str, *, charge_audit: dict | None = None, parameter_hashes: list[str] | None = None) -> dict:
    return artifact("ff", "forcefield_bundle", [parent], metadata={
        "forcefield_profile": "reviewed-profile",
        "profile_sha256": HASH_B,
        "parameter_file_sha256": parameter_hashes if parameter_hashes is not None else [HASH],
        "atom_typing_coverage": 1,
        "charge_audit": charge_audit or {"status": "VERIFIED", "partial_charge_coverage": 1, "net_charge_e": 0.0, "expected_net_charge_e": 0.0, "tolerance_e": 1e-6},
    })


class ProjectManifestV2Tests(unittest.TestCase):
    def test_schema_lists_required_artifact_types_and_states(self) -> None:
        path = Path(__file__).resolve().parents[1] / "config" / "project-manifest.schema.v2.json"
        schema = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(schema["properties"]["schema_version"]["const"], 2)
        self.assertEqual(schema["$defs"]["artifact"]["properties"]["artifact_type"]["enum"], [
            "source_structure", "derived_structure", "forcefield_bundle", "conversion_artifact", "simulation_run", "analysis_result", "evidence_receipt"
        ])

    def test_missing_hash_is_rejected(self) -> None:
        manifest = new_manifest_v2("hash", "Hash")
        bad = artifact("source", "source_structure", [])
        bad["sha256"] = "missing"
        with self.assertRaisesRegex(ValueError, "sha256"):
            register_artifact_v2(manifest, bad)

    def test_missing_parent_artifact_is_rejected(self) -> None:
        manifest = new_manifest_v2("parent", "Parent")
        with self.assertRaisesRegex(ValueError, "parent artifact"):
            register_artifact_v2(manifest, artifact("derived", "derived_structure", ["not-registered"]))

    def test_forcefield_parameter_hash_and_charge_audit_are_required(self) -> None:
        manifest = new_manifest_v2("ff", "Forcefield")
        source = artifact("source", "source_structure", [])
        manifest = register_artifact_v2(manifest, source)
        with self.assertRaisesRegex(ValueError, "parameter_file_sha256"):
            register_artifact_v2(manifest, forcefield("source", parameter_hashes=[]))
        with self.assertRaisesRegex(ValueError, "charge audit"):
            register_artifact_v2(manifest, forcefield("source", charge_audit={"status": "UNVERIFIED", "partial_charge_coverage": 1, "net_charge_e": 0.0, "expected_net_charge_e": 0.0}))

    def test_illegal_skipped_and_backward_transitions_are_rejected(self) -> None:
        manifest = new_manifest_v2("states", "States")
        with self.assertRaisesRegex(ValueError, "illegal state transition"):
            transition_project_state_v2(manifest, "FORCEFIELD_VERIFIED")
        manifest = transition_project_state_v2(manifest, "BLOCKED", blockers=["needs review"])
        with self.assertRaisesRegex(ValueError, "illegal state transition"):
            transition_project_state_v2(manifest, "DRAFT")
        with self.assertRaisesRegex(ValueError, "illegal state transition"):
            transition_project_state_v2(manifest, "BLOCKED", blockers=[])

    def _preflight_manifest(self) -> tuple[dict, dict[str, dict]]:
        manifest = new_manifest_v2("preflight", "Preflight")
        source = artifact("source", "source_structure", [])
        manifest = register_artifact_v2(manifest, source)
        manifest = set_target_artifact_v2(manifest, "source")
        struct_ev = evidence("ev-structure", "source", "STRUCTURE_VERIFIED", kind="MODEL_GEOMETRY")
        manifest = register_artifact_v2(manifest, struct_ev)
        manifest = transition_project_state_v2(manifest, "STRUCTURE_VERIFIED", evidence_ids=["ev-structure"])
        ff = forcefield("source")
        manifest = register_artifact_v2(manifest, ff)
        ff_ev = evidence("ev-ff", "ff", "FORCEFIELD_VERIFIED", kind="MODEL_GEOMETRY")
        manifest = register_artifact_v2(manifest, ff_ev)
        manifest = transition_project_state_v2(manifest, "FORCEFIELD_VERIFIED", evidence_ids=["ev-ff"])
        conversion = artifact("conversion", "conversion_artifact", ["ff"])
        manifest = register_artifact_v2(manifest, conversion)
        conversion_ev = evidence("ev-conversion", "conversion", "CONVERSION_VERIFIED", kind="INTERFACE_CONVERSION")
        manifest = register_artifact_v2(manifest, conversion_ev)
        manifest = transition_project_state_v2(manifest, "CONVERSION_VERIFIED", evidence_ids=["ev-conversion"])
        preflight_ev = evidence("ev-preflight", "conversion", "LAMMPS_PREFLIGHT_VERIFIED")
        manifest = register_artifact_v2(manifest, preflight_ev)
        manifest = transition_project_state_v2(manifest, "LAMMPS_PREFLIGHT_VERIFIED", evidence_ids=["ev-preflight"])
        return manifest, {"source": source, "ff": ff, "conversion": conversion, "preflight": preflight_ev}

    def test_candidate_cannot_authorize_qualification(self) -> None:
        manifest, _ = self._preflight_manifest()
        candidate = evidence("candidate", "conversion", "QUALIFICATION_ONLY", result="CANDIDATE", status="CANDIDATE")
        manifest = register_artifact_v2(manifest, candidate)
        with self.assertRaisesRegex(PermissionError, "QUALIFICATION_ONLY"):
            transition_project_state_v2(manifest, "QUALIFICATION_ONLY", evidence_ids=["candidate"])

    def test_software_or_conversion_evidence_cannot_authorize_production(self) -> None:
        manifest, _ = self._preflight_manifest()
        with self.assertRaisesRegex(ValueError, "production evidence"):
            transition_project_state_v2(manifest, "PRODUCTION_READY", evidence_ids=["ev-preflight"], authorized_by="user", manual_authorization=True)

    def test_target_model_must_not_use_calibration_evidence(self) -> None:
        manifest, _ = self._preflight_manifest()
        calibration = evidence("calibration", "source", "PRODUCTION_READY", kind="SCIENTIFIC_PRODUCTION", role="calibration", status="PRODUCTION_APPROVED")
        manifest = register_artifact_v2(manifest, calibration)
        with self.assertRaisesRegex(ValueError, "production evidence"):
            transition_project_state_v2(manifest, "PRODUCTION_READY", evidence_ids=["calibration"], authorized_by="user", manual_authorization=True)

    def test_production_state_cannot_be_declared_without_authorized_history(self) -> None:
        manifest, _ = self._preflight_manifest()
        forged = deepcopy(manifest)
        forged["project"]["state"] = "PRODUCTION_READY"
        with self.assertRaisesRegex(ValueError, "state does not match"):
            validate_manifest_v2(forged)

    def test_qualification_is_not_production(self) -> None:
        manifest, _ = self._preflight_manifest()
        qualification = evidence("qualification", "conversion", "QUALIFICATION_ONLY", status="QUALIFICATION_ONLY")
        manifest = register_artifact_v2(manifest, qualification)
        manifest = transition_project_state_v2(manifest, "QUALIFICATION_ONLY", evidence_ids=["qualification"])
        self.assertEqual(manifest["project"]["state"], "QUALIFICATION_ONLY")
        self.assertFalse(manifest["project"]["state"] == "PRODUCTION_READY")


if __name__ == "__main__":
    unittest.main()
