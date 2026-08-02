from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from materials_studio_mcp import castep_real_qualification_plan as real_plan
from materials_studio_mcp.capability_registry import load_capability_registry
from materials_studio_mcp.castep_standalone import prepare_castep_standalone_inputs


def _write_xsd(path: Path, *, one_atom: bool = False) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    atoms = '<Atom3d ID="1" Components="Si" XYZ="0.480780717969765,0.480780717969765,0" />'
    if not one_atom:
        atoms += '<Atom3d ID="2" Components="O" XYZ="0.150179395584351,0.41458896285322,0.11649944465013" />'
    path.write_text(
        "<XSD Version='23.1'>" + atoms
        + '<SpaceGroup ITNumber="154" GroupName="P3221" '
        'Operators="1,0,0,0,0,1,0,0,0,0,1,0:0,-1,0,0,1,-1,0,0,0,0,1,0.666666666666667:'
        '-1,1,0,0,-1,0,0,0,0,0,1,0.333333333333333:0,1,0,0,1,0,0,0,0,0,-1,0:'
        '1,-1,0,0,0,-1,0,0,0,0,-1,0.333333333333333:-1,0,0,0,-1,1,0,0,0,0,-1,0.666666666666667" '
        'AVector="4.25218118314589,-2.45499795073234,0" BVector="0,4.90999590146468,0" CVector="0,0,5.402" /></XSD>',
        encoding="utf-8",
    )
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def _context(digest: str, *, cutoff: float = 600.0) -> dict:
    return {
        "schema_version": 1,
        "task": "single_point",
        "purpose": "preliminary",
        "input_sha256": digest,
        "electronic_character": {"value": "insulator", "source": "P3-A test"},
        "magnetism": {"value": "nonmagnetic", "source": "P3-A test"},
        "dispersion": {"value": "off", "source": "P3-A test"},
        "pseudopotentials": {"value": "default_otfg", "source": "P3-A test"},
        "xc_functional": {"value": "PBE", "source": "P3-A test"},
        "energy_cutoff_ev": {"value": cutoff, "source": "qualification candidate"},
        "kpoint_mp_grid": {"value": [3, 3, 3], "source": "qualification candidate"},
        "convergence_evidence": [],
    }


class RealCastepQualificationPlanTests(unittest.TestCase):
    def _manifest(self, root: Path, *, name: str = "quartz_alpha", cutoff: float = 600.0, one_atom: bool = False) -> Path:
        source = root / "source.xsd"
        digest = _write_xsd(source, one_atom=one_atom)
        result = prepare_castep_standalone_inputs(
            input_xsd=source,
            input_sha256=digest,
            output_directory=root / "candidate",
            calculation_name=name,
            standalone_context=_context(digest, cutoff=cutoff),
            cores=4,
            dry_run=False,
        )
        return Path(result["manifest"]["path"])

    def test_reviewed_plan_is_deterministic_and_execution_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = self._manifest(Path(temporary))
            first = real_plan.build_real_castep_qualification_plan(input_manifest=manifest)
            second = real_plan.build_real_castep_qualification_plan(input_manifest=manifest)
        self.assertEqual(first, second)
        self.assertFalse(first["execution_allowed"])
        self.assertEqual(first["input"]["runtime_atom_count"], 9)
        self.assertEqual(first["input"]["elements"], {"O": 6, "Si": 3})
        self.assertEqual(first["runtime"]["cores"], 4)
        self.assertEqual(first["runtime"]["hard_timeout_seconds"], 600)
        real_plan.validate_real_castep_qualification_plan(first)

    def test_launcher_and_documentation_hash_drift_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = self._manifest(Path(temporary))
            with patch.object(real_plan, "_RUNCASTEP_SHA256", "F" * 64):
                with self.assertRaises(PermissionError):
                    real_plan.build_real_castep_qualification_plan(input_manifest=manifest)
            with patch.object(real_plan, "_RUNCASTEP_README_SHA256", "E" * 64):
                with self.assertRaises(PermissionError):
                    real_plan.build_real_castep_qualification_plan(input_manifest=manifest)

    def test_command_interpreter_hash_drift_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = self._manifest(Path(temporary))
            with patch.object(real_plan, "_CMD_SHA256", "D" * 64):
                with self.assertRaises(PermissionError):
                    real_plan.build_real_castep_qualification_plan(input_manifest=manifest)

    def test_command_injection_seed_is_rejected_before_preview(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = self._manifest(Path(temporary), name="quartz & calc.exe")
            with self.assertRaises(ValueError):
                real_plan.build_real_castep_qualification_plan(input_manifest=manifest)

    def test_unicode_qualification_root_fails_before_any_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = self._manifest(Path(temporary))
            unicode_root = Path(temporary) / "非ASCII"
            with patch.object(real_plan, "_QUALIFICATION_ROOT", unicode_root):
                with self.assertRaises(ValueError):
                    real_plan.build_real_castep_qualification_plan(input_manifest=manifest)
            self.assertFalse(unicode_root.exists())

    def test_unreviewed_settings_and_material_scope_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cutoff_manifest = self._manifest(root / "cutoff", cutoff=550.0)
            atom_manifest = self._manifest(root / "atom", one_atom=True)
            with self.assertRaises(ValueError):
                real_plan.build_real_castep_qualification_plan(input_manifest=cutoff_manifest)
            with self.assertRaises(ValueError):
                real_plan.build_real_castep_qualification_plan(input_manifest=atom_manifest)

    def test_input_hash_drift_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = self._manifest(Path(temporary))
            param = manifest.parent / "quartz_alpha_sp_4c.param"
            param.write_text("tampered\n", encoding="ascii")
            with self.assertRaises(ValueError):
                real_plan.build_real_castep_qualification_plan(input_manifest=manifest)

    def test_plan_hash_detects_any_tamper(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = self._manifest(Path(temporary))
            plan = real_plan.build_real_castep_qualification_plan(input_manifest=manifest)
        plan["runtime"]["hard_timeout_seconds"] = 601
        with self.assertRaises(ValueError):
            real_plan.validate_real_castep_qualification_plan(plan)

    def test_real_execution_entry_is_permanently_blocked_without_subprocess(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = self._manifest(Path(temporary))
            plan = real_plan.build_real_castep_qualification_plan(input_manifest=manifest)
        with patch("subprocess.Popen") as popen:
            result = real_plan.execute_real_castep_qualification(plan=plan)
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["executed"])
        popen.assert_not_called()

    def test_plan_build_performs_no_job_directory_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self._manifest(root)
            before = sorted(path.relative_to(root) for path in root.rglob("*"))
            real_plan.build_real_castep_qualification_plan(input_manifest=manifest)
            after = sorted(path.relative_to(root) for path in root.rglob("*"))
        self.assertEqual(before, after)

    def test_registry_keeps_p3a_private_and_public_castep_unverified(self) -> None:
        capabilities = {item["id"]: item for item in load_capability_registry()["capabilities"]}
        candidate = capabilities["castep.real_qualification_plan_candidate"]
        self.assertEqual(candidate["status"], "todo")
        self.assertFalse(candidate["verified"])
        self.assertEqual(candidate["exposure"], "not_implemented")
        self.assertEqual(capabilities["castep.calculation"]["status"], "unverified")
        self.assertEqual(capabilities["results.castep_parsing"]["status"], "unverified")


if __name__ == "__main__":
    unittest.main()
