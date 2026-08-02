from __future__ import annotations

import hashlib
import shutil
import tempfile
import unittest
from pathlib import Path

from materials_studio_mcp.capability_registry import load_capability_registry
from materials_studio_mcp.castep_result_parser import PARSER_REVISION, parse_standalone_castep_result
from materials_studio_mcp.castep_standalone import prepare_castep_standalone_inputs


FIXTURES = Path(__file__).parent / "fixtures" / "castep_result_parser"
EXPECTED_RESULT_KEYS = {
    "schema_version", "parser_revision", "status", "classification", "completion_evidence",
    "energy", "warnings", "errors", "blockers", "input_hashes", "output_hashes",
    "seedname", "process",
}


def write_periodic_xsd(path: Path) -> str:
    path.write_text(
        "<XSD Version='23.1'>"
        '<Atom3d ID="1" Components="Si" XYZ="0,0,0" />'
        '<Atom3d ID="2" Components="O" XYZ="0.25,0.25,0.25" />'
        '<Atom3d ID="3" ImageOf="1" />'
        '<SpaceGroup ITNumber="225" GroupName="FM-3M" '
        'Operators="1,0,0,0,0,1,0,0,0,0,1,0:1,0,0,0.5,0,1,0,0.5,0,0,1,0:'
        '1,0,0,0,0,1,0,0.5,0,0,1,0.5:1,0,0,0.5,0,1,0,0,0,0,1,0.5" '
        'AVector="5,0,0" BVector="0,5,0" CVector="0,0,5" />'
        "</XSD>",
        encoding="utf-8",
    )
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def standalone_context(digest: str) -> dict:
    return {
        "schema_version": 1,
        "task": "single_point",
        "purpose": "preliminary",
        "input_sha256": digest,
        "electronic_character": {"value": "insulator", "source": "parser test"},
        "magnetism": {"value": "nonmagnetic", "source": "parser test"},
        "dispersion": {"value": "off", "source": "parser test"},
        "pseudopotentials": {"value": "default_otfg", "source": "parser test"},
        "xc_functional": {"value": "PBE", "source": "parser test"},
        "energy_cutoff_ev": {"value": 600.0, "source": "parser test"},
        "kpoint_mp_grid": {"value": [3, 3, 3], "source": "parser test"},
        "convergence_evidence": [],
    }


class CastepResultParserTests(unittest.TestCase):
    def _candidate(self, root: Path) -> tuple[dict, Path]:
        root.mkdir(parents=True, exist_ok=True)
        source = root / "source.xsd"
        digest = write_periodic_xsd(source)
        candidate = prepare_castep_standalone_inputs(
            input_xsd=source,
            input_sha256=digest,
            output_directory=root / "candidate",
            calculation_name="parser candidate",
            standalone_context=standalone_context(digest),
            dry_run=False,
        )
        return candidate, Path(candidate["manifest"]["path"])

    def _output(self, candidate: dict, fixture: str) -> Path:
        destination = Path(candidate["output_directory"]) / f"{candidate['seedname']}.castep"
        shutil.copyfile(FIXTURES / fixture, destination)
        return destination

    def test_completed_requires_contract_and_finite_completion_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            candidate, manifest = self._candidate(Path(temporary))
            output = self._output(candidate, "completed.castep")
            digest = hashlib.sha256(output.read_bytes()).hexdigest().upper()
            result = parse_standalone_castep_result(
                castep_output=output,
                input_manifest=manifest,
                expected_output_sha256=digest,
                process_exit_code=9,
            )
        self.assertEqual(set(result), EXPECTED_RESULT_KEYS)
        self.assertEqual(result["parser_revision"], PARSER_REVISION)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["classification"], "completed")
        self.assertEqual(result["energy"], {"unit": "eV", "value": -12.345678})
        self.assertEqual(result["output_hashes"]["observed_sha256"], digest)
        self.assertTrue(result["input_hashes"]["contract_canonical_sha256"])
        self.assertIn("NONZERO_EXIT_CODE", [item["code"] for item in result["warnings"]])
        self.assertIn("CASTEP_EXECUTION_UNVERIFIED", [item["code"] for item in result["blockers"]])

    def test_log_failure_markers_override_zero_exit_code(self) -> None:
        cases = {
            "license_unavailable.castep": "license_unavailable",
            "scf_not_converged.castep": "scf_not_converged",
            "fatal_error.castep": "fatal_error",
            "timeout.castep": "timeout",
            "cancelled.castep": "cancelled",
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for number, (fixture, classification) in enumerate(cases.items()):
                with self.subTest(fixture=fixture):
                    candidate, manifest = self._candidate(root / str(number))
                    result = parse_standalone_castep_result(
                        castep_output=self._output(candidate, fixture), input_manifest=manifest, process_exit_code=0
                    )
                    self.assertEqual(result["status"], "failed")
                    self.assertEqual(result["classification"], classification)
                    self.assertIsNone(result["energy"])

    def test_explicit_timeout_and_cancellation_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for number, termination in enumerate(("timeout", "cancelled")):
                with self.subTest(termination=termination):
                    candidate, manifest = self._candidate(root / termination)
                    result = parse_standalone_castep_result(
                        castep_output=self._output(candidate, "truncated.castep"),
                        input_manifest=manifest,
                        termination=termination,
                    )
                    self.assertEqual(result["classification"], termination)

    def test_missing_completion_evidence_is_classified_as_truncated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            candidate, manifest = self._candidate(Path(temporary))
            result = parse_standalone_castep_result(
                castep_output=self._output(candidate, "truncated.castep"), input_manifest=manifest
            )
        self.assertEqual(result["classification"], "output_truncated")
        self.assertIsNone(result["energy"])

    def test_nan_and_infinity_never_produce_an_energy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for token in ("NaN", "Infinity"):
                with self.subTest(token=token):
                    candidate, manifest = self._candidate(root / token)
                    output = Path(candidate["output_directory"]) / f"{candidate['seedname']}.castep"
                    output.write_text(f"Final energy = {token} eV\nTotal time = 1.0 s\n", encoding="utf-8")
                    result = parse_standalone_castep_result(castep_output=output, input_manifest=manifest)
                    self.assertEqual(result["classification"], "nonfinite_numeric")
                    self.assertIsNone(result["energy"])

    def test_conflicting_completion_and_error_markers_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            candidate, manifest = self._candidate(Path(temporary))
            result = parse_standalone_castep_result(
                castep_output=self._output(candidate, "conflicting.castep"), input_manifest=manifest
            )
        self.assertEqual(result["classification"], "conflicting_markers")
        self.assertIsNone(result["energy"])

    def test_same_named_output_requires_external_hash_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            candidate, manifest = self._candidate(Path(temporary))
            output = self._output(candidate, "completed.castep")
            unbound = parse_standalone_castep_result(castep_output=output, input_manifest=manifest)
            bound = parse_standalone_castep_result(
                castep_output=output,
                input_manifest=manifest,
                expected_output_sha256=hashlib.sha256(output.read_bytes()).hexdigest(),
            )
        self.assertEqual(unbound["status"], "failed")
        self.assertEqual(unbound["classification"], "output_unbound")
        self.assertEqual(bound["status"], "completed")

    def test_old_output_seed_cannot_be_mixed_with_current_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            candidate, manifest = self._candidate(Path(temporary))
            stale = Path(candidate["output_directory"]) / "old_calculation.castep"
            shutil.copyfile(FIXTURES / "completed.castep", stale)
            result = parse_standalone_castep_result(castep_output=stale, input_manifest=manifest)
        self.assertEqual(result["classification"], "seed_contract_mismatch")

    def test_tampered_input_and_output_hashes_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate, manifest = self._candidate(root / "input")
            output = self._output(candidate, "completed.castep")
            Path(candidate["cell"]["path"]).write_text("tampered\n", encoding="ascii")
            tampered_input = parse_standalone_castep_result(castep_output=output, input_manifest=manifest)
            self.assertEqual(tampered_input["classification"], "input_integrity_failure")

            candidate, manifest = self._candidate(root / "output")
            output = self._output(candidate, "completed.castep")
            wrong_hash = "0" * 64
            tampered_output = parse_standalone_castep_result(
                castep_output=output, input_manifest=manifest, expected_output_sha256=wrong_hash
            )
            self.assertEqual(tampered_output["classification"], "output_integrity_failure")

    def test_private_candidate_registry_does_not_release_public_parsing(self) -> None:
        capabilities = {item["id"]: item for item in load_capability_registry()["capabilities"]}
        private = capabilities["results.standalone_castep_parser_qualification"]
        self.assertEqual(private["status"], "todo")
        self.assertFalse(private["verified"])
        self.assertEqual(private["exposure"], "not_implemented")
        public = capabilities["results.castep_parsing"]
        self.assertEqual(public["status"], "unverified")
        self.assertFalse(public["verified"])
        self.assertEqual(public["exposure"], "not_implemented")


if __name__ == "__main__":
    unittest.main()
