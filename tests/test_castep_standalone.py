from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from materials_studio_mcp import castep_standalone
from materials_studio_mcp.castep_standalone import prepare_castep_standalone_inputs


WORKSPACE_TMP = Path(r"D:\分子动力学模拟\tmp")


def write_periodic_xsd(path: Path) -> str:
    path.write_text(
        "<XSD Version='23.1'>"
        '<Atom3d ID="1" Components="Si" XYZ="0,0,0" />'
        '<Atom3d ID="2" Components="O" XYZ="0.25,0.25,0.25" />'
        '<Atom3d ID="3" ImageOf="1" />'
        '<SpaceGroup ITNumber="225" GroupName="FM-3M" '
        'Operators="1,0,0,0,0,1,0,0,0,0,1,0:'
        '1,0,0,0.5,0,1,0,0.5,0,0,1,0:'
        '1,0,0,0,0,1,0,0.5,0,0,1,0.5:'
        '1,0,0,0.5,0,1,0,0,0,0,1,0.5" '
        'AVector="5,0,0" BVector="0,5,0" CVector="0,0,5" />'
        "</XSD>",
        encoding="utf-8",
    )
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def standalone_context(digest: str, *, purpose: str = "preliminary", convergence: list[str] | None = None) -> dict:
    return {
        "schema_version": 1,
        "task": "single_point",
        "purpose": purpose,
        "input_sha256": digest,
        "electronic_character": {"value": "insulator", "source": "reviewed test structure"},
        "magnetism": {"value": "nonmagnetic", "source": "reviewed test structure"},
        "dispersion": {"value": "off", "source": "explicit test scope"},
        "pseudopotentials": {"value": "default_otfg", "source": "MS 2023 SPECIES_POT help"},
        "xc_functional": {"value": "PBE", "source": "explicit test setting"},
        "energy_cutoff_ev": {"value": 600.0, "source": "explicit provisional test setting"},
        "kpoint_mp_grid": {"value": [3, 3, 3], "source": "explicit provisional test setting"},
        "convergence_evidence": [] if convergence is None else convergence,
    }


class CastepStandaloneInputTests(unittest.TestCase):
    def test_dry_run_is_hash_bound_and_creates_no_directory(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_TMP) as temporary:
            root = Path(temporary)
            source = root / "source.xsd"
            digest = write_periodic_xsd(source)
            output = root / "candidate"
            result = prepare_castep_standalone_inputs(
                input_xsd=source,
                input_sha256=digest,
                output_directory=output,
                calculation_name="quartz candidate",
                standalone_context=standalone_context(digest),
            )
            self.assertEqual(result["status"], "dry_run")
            self.assertFalse(result["execution_allowed"])
            self.assertFalse(result["execution_started"])
            self.assertFalse(result["gateway_selected"])
            self.assertFalse(result["writes_performed"])
            self.assertFalse(output.exists())
            self.assertEqual(result["source"]["runtime_atom_count"], 8)

    def test_prepared_candidate_contains_only_explicit_supported_inputs_and_hashes(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_TMP) as temporary:
            root = Path(temporary)
            source = root / "source.xsd"
            digest = write_periodic_xsd(source)
            output = root / "candidate"
            result = prepare_castep_standalone_inputs(
                input_xsd=source,
                input_sha256=digest,
                output_directory=output,
                calculation_name="quartz candidate",
                standalone_context=standalone_context(digest),
                cores=4,
                dry_run=False,
            )
            self.assertEqual(result["status"], "prepared")
            self.assertFalse(result["execution_allowed"])
            self.assertTrue(result["writes_performed"])
            self.assertEqual(result["input_source_copy"]["sha256"], digest)
            self.assertEqual(result["cell"]["sha256"], hashlib.sha256(Path(result["cell"]["path"]).read_bytes()).hexdigest().upper())
            self.assertEqual(result["param"]["sha256"], hashlib.sha256(Path(result["param"]["path"]).read_bytes()).hexdigest().upper())
            self.assertTrue(Path(result["manifest"]["path"]).is_file())

            cell = Path(result["cell"]["path"]).read_text(encoding="ascii")
            param = Path(result["param"]["path"]).read_text(encoding="ascii")
            contract = json.loads(Path(result["contract"]["path"]).read_text(encoding="utf-8"))
            manifest = json.loads(Path(result["manifest"]["path"]).read_text(encoding="utf-8"))
            self.assertIn("%BLOCK LATTICE_CART", cell)
            self.assertIn("%BLOCK POSITIONS_FRAC", cell)
            self.assertIn("KPOINTS_MP_GRID 3 3 3", cell)
            self.assertEqual(sum(1 for line in cell.splitlines() if line.startswith("  Si") or line.startswith("  O ")), 8)
            self.assertNotIn("SPECIES_POT", cell)
            self.assertIn("TASK : SinglePoint", param)
            self.assertIn("XC_FUNCTIONAL : PBE", param)
            self.assertIn("CUT_OFF_ENERGY : 600.0000000000 eV", param)
            self.assertIn("SPIN_POLARIZED : FALSE", param)
            self.assertIn("FIX_OCCUPANCY : TRUE", param)
            self.assertFalse(contract["execution_allowed"])
            self.assertEqual(manifest["contract_sha256"], result["contract"]["canonical_sha256"])
            forbidden = [
                path for path in output.iterdir()
                if path.suffix.lower() in {".castep", ".check", ".castep_bin"}
            ]
            self.assertEqual(forbidden, [])

    def test_closed_scientific_scope_and_core_limit_fail_before_writes(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_TMP) as temporary:
            root = Path(temporary)
            source = root / "source.xsd"
            digest = write_periodic_xsd(source)
            context = standalone_context(digest)
            context["magnetism"] = {"value": "collinear", "source": "not R1"}
            with self.assertRaisesRegex(ValueError, "magnetism=nonmagnetic"):
                prepare_castep_standalone_inputs(
                    input_xsd=source,
                    input_sha256=digest,
                    output_directory=root / "not-written",
                    calculation_name="invalid",
                    standalone_context=context,
                )
            self.assertFalse((root / "not-written").exists())
            with self.assertRaisesRegex(ValueError, "1 to 4"):
                prepare_castep_standalone_inputs(
                    input_xsd=source,
                    input_sha256=digest,
                    output_directory=root / "not-written-cores",
                    calculation_name="invalid",
                    standalone_context=standalone_context(digest),
                    cores=5,
                )

    def test_research_candidate_requires_explicit_convergence_evidence(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_TMP) as temporary:
            root = Path(temporary)
            source = root / "source.xsd"
            digest = write_periodic_xsd(source)
            with self.assertRaisesRegex(ValueError, "convergence_evidence"):
                prepare_castep_standalone_inputs(
                    input_xsd=source,
                    input_sha256=digest,
                    output_directory=root / "research-candidate",
                    calculation_name="research",
                    standalone_context=standalone_context(digest, purpose="research"),
                )

    def test_changed_source_snapshot_fails_closed_and_removes_candidate_directory(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_TMP) as temporary:
            root = Path(temporary)
            source = root / "source.xsd"
            digest = write_periodic_xsd(source)
            output = root / "candidate"
            original_copy = castep_standalone.shutil.copy2

            def changed_copy(src: Path, dst: Path) -> Path:
                result = original_copy(src, dst)
                dst.write_bytes(dst.read_bytes() + b"changed-after-validation")
                return result

            with patch.object(castep_standalone.shutil, "copy2", side_effect=changed_copy):
                with self.assertRaisesRegex(RuntimeError, "Copied XSD changed"):
                    prepare_castep_standalone_inputs(
                        input_xsd=source,
                        input_sha256=digest,
                        output_directory=output,
                        calculation_name="toctou",
                        standalone_context=standalone_context(digest),
                        dry_run=False,
                    )
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
